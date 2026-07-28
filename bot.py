import os
import sys
import asyncio
import re
import time
import subprocess
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID"))
OWNER_GROUP = int(os.getenv("OWNER_GROUP"))

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(BASE_DIR),
)

# ---------------------------------------------------------------------------
# Shared progress state  (task_id -> dict with phase/current/total)
# Updated by download monitor & upload callback, read by progress updater.
# ---------------------------------------------------------------------------

progress_data = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_bytes(size):
    """Convert byte count to human-readable string."""
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    size = float(size)
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"


def extract_url(text):
    """Return the first HTTP(S) URL found in text, or None."""
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def is_video_url(url):
    """Check whether the URL looks like a video / HLS stream link."""
    keywords = ["m3u8", "video", "stream", "mp4", "hls", ".ts"]
    lower = url.lower()
    return any(kw in lower for kw in keywords)


# ---------------------------------------------------------------------------
# Owner-group-only filter
# ---------------------------------------------------------------------------


def _owner_group_check(_, __, message):
    return message.chat and message.chat.id == OWNER_GROUP


owner_filter = filters.create(_owner_group_check)

# ---------------------------------------------------------------------------
# Background progress message updater
# ---------------------------------------------------------------------------


async def _progress_loop(status_msg, task_id):
    """Edit the status message every 3 seconds with current progress.

    Runs as a fire-and-forget asyncio task so it never blocks or slows
    down the actual download / upload happening in the main coroutine.
    """
    last_text = ""
    while task_id in progress_data:
        info = progress_data.get(task_id)
        if info is None:
            break

        phase = info.get("phase", "")
        current = info.get("current", 0)
        total = info.get("total", 0)

        if total > 0:
            text = f"{phase}\n\n{format_bytes(current)} / {format_bytes(total)}"
        else:
            text = f"{phase}\n\n{format_bytes(current)}"

        if text != last_text:
            try:
                await status_msg.edit_text(text)
                last_text = text
            except Exception:
                pass

        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# FFmpeg HLS downloader
# ---------------------------------------------------------------------------


async def _ffmpeg_download(url, output_path, task_id):
    """Download an HLS / m3u8 stream with ffmpeg and return the exit code.

    A lightweight monitor polls the growing output file every 0.5 s and
    writes the current size into progress_data so the UI updater can
    pick it up independently.
    """
    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
        "Referer: https://classx.co.in/\r\n"
        "Origin: https://classx.co.in\r\n"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-headers", headers,
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    # Monitor output file size growth (non-blocking, lightweight)
    async def _monitor():
        while task_id in progress_data:
            try:
                if os.path.exists(output_path):
                    progress_data[task_id]["current"] = os.path.getsize(output_path)
            except OSError:
                pass
            await asyncio.sleep(0.5)

    monitor = asyncio.create_task(_monitor())

    _, stderr_bytes = await process.communicate()

    # Stop the monitor
    progress_data.pop(task_id, None)
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass

    return process.returncode, stderr_bytes.decode(errors="replace")


# ---------------------------------------------------------------------------
# Pyrogram upload progress callback
# ---------------------------------------------------------------------------


def _upload_progress(current, total, task_id):
    """Called by pyrogram during upload.  Just updates the shared dict."""
    if task_id in progress_data:
        progress_data[task_id]["current"] = current
        progress_data[task_id]["total"] = total


# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("start"))
async def handle_start(client, message):
    await message.reply_text(
        "Bot is running.\n\n"
        "Send an m3u8 / HLS video link to download and upload.\n\n"
        "Commands:\n"
        "/start  - Show this message\n"
        "/update - Pull from GitHub and restart"
    )


# ---------------------------------------------------------------------------
# /update command
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("update"))
async def handle_update(client, message):
    status = await message.reply_text("Pulling from GitHub...")

    result = subprocess.run(
        ["git", "pull"],
        capture_output=True,
        text=True,
        cwd=str(BASE_DIR),
    )

    output = (result.stdout.strip() or result.stderr.strip() or "No output")
    await status.edit_text(f"Update result:\n\n{output}\n\nRestarting...")

    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# Link handler  (video download + upload)
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.text & ~filters.command(["update", "start", "help"]))
async def handle_link(client, message):
    url = extract_url(message.text)
    if url is None:
        return

    if not is_video_url(url):
        return

    task_id = f"{message.chat.id}_{message.id}_{int(time.time())}"
    filename = f"video_{message.id}_{int(time.time())}.mp4"
    output_path = str(DOWNLOAD_DIR / filename)

    # -- Download phase ----------------------------------------------------

    status_msg = await message.reply_text("Downloading\n\n0 B")

    progress_data[task_id] = {
        "phase": "Downloading",
        "current": 0,
        "total": 0,
    }

    updater = asyncio.create_task(_progress_loop(status_msg, task_id))

    try:
        return_code, stderr = await _ffmpeg_download(url, output_path, task_id)

        if return_code != 0 or not os.path.exists(output_path):
            updater.cancel()
            error_lines = stderr.strip().split("\n")[-3:]
            await status_msg.edit_text(
                "Download failed\n\n" + "\n".join(error_lines)
            )
            return

        file_size = os.path.getsize(output_path)

        if file_size == 0:
            updater.cancel()
            await status_msg.edit_text("Download failed: file is empty")
            return

        # -- Upload phase --------------------------------------------------

        progress_data[task_id] = {
            "phase": "Uploading",
            "current": 0,
            "total": file_size,
        }

        # The updater task is still running; it will now pick up the new phase.

        await client.send_video(
            chat_id=message.chat.id,
            video=output_path,
            file_name=filename,
            supports_streaming=True,
            progress=_upload_progress,
            progress_args=(task_id,),
        )

        # -- Done ----------------------------------------------------------

        progress_data.pop(task_id, None)
        updater.cancel()

        await status_msg.edit_text(
            f"Completed\n\nFile: {filename}\nSize: {format_bytes(file_size)}"
        )

    except Exception as exc:
        progress_data.pop(task_id, None)
        updater.cancel()
        await status_msg.edit_text(f"Error: {exc}")

    finally:
        # Always clean up the local file
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick sanity check
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("ERROR: BOT_TOKEN, API_ID, or API_HASH missing from .env")
        sys.exit(1)

    print("Bot started. Listening in owner group only.")
    app.run()
