import os
import sys
import asyncio
import re
import time
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://classx.co.in/",
    "Origin": "https://classx.co.in",
}

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


def check_token_expiry(url):
    """Check if the edge-cache-token in the URL has expired.

    Returns (is_expired: bool, expiry_ist_str: str or None).
    If no Expires field is found, returns (False, None) so download proceeds.
    """
    match = re.search(r"Expires=(\d+)", url)
    if not match:
        return False, None

    expires_ts = int(match.group(1))
    now_ts = int(time.time())
    ist = timezone(timedelta(hours=5, minutes=30))
    expiry_dt = datetime.fromtimestamp(expires_ts, tz=ist)
    expiry_str = expiry_dt.strftime("%d-%m-%Y %I:%M %p IST")

    if now_ts > expires_ts:
        return True, expiry_str
    return False, expiry_str


def check_url_accessible(url):
    """Test if the URL is accessible. Returns (ok, status_code, error_msg)."""
    try:
        req = Request(url)
        for key, val in HEADERS.items():
            req.add_header(key, val)
        resp = urlopen(req, timeout=15)
        resp.read(1024)  # Read a small chunk to verify
        resp.close()
        return True, resp.status, None
    except HTTPError as e:
        return False, e.code, f"HTTP {e.code}: {e.reason}"
    except URLError as e:
        return False, 0, f"URL Error: {e.reason}"
    except Exception as e:
        return False, 0, str(e)


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
# Video downloader (yt-dlp primary, ffmpeg fallback)
# ---------------------------------------------------------------------------


async def _download_video(url, output_path, task_id):
    """Download HLS video. Tries yt-dlp first, falls back to ffmpeg.

    Returns (success: bool, error_msg: str).
    """
    # Build ffmpeg headers string for both yt-dlp and ffmpeg
    ffmpeg_headers = "".join(f"{k}: {v}\r\n" for k, v in HEADERS.items())

    # --- Try yt-dlp first ---
    ytdlp_cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--add-header", f"User-Agent: {HEADERS['User-Agent']}",
        "--add-header", f"Referer: {HEADERS['Referer']}",
        "--add-header", f"Origin: {HEADERS['Origin']}",
        "-o", output_path,
        url,
    ]

    print(f"[DOWNLOAD] Trying yt-dlp...")
    process = await asyncio.create_subprocess_exec(
        *ytdlp_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    # Monitor file size in background
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

    if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        monitor.cancel()
        print(f"[DOWNLOAD] yt-dlp succeeded")
        return True, ""

    ytdlp_err = stderr_bytes.decode(errors="replace").strip()
    print(f"[DOWNLOAD] yt-dlp failed: {ytdlp_err[-200:]}")

    # --- Fallback to ffmpeg ---
    print(f"[DOWNLOAD] Falling back to ffmpeg...")

    # Clean up any partial yt-dlp output
    for f in DOWNLOAD_DIR.glob(f"{Path(output_path).stem}*"):
        try:
            f.unlink()
        except OSError:
            pass

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-headers", ffmpeg_headers,
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr_bytes = await process.communicate()

    # Stop monitor
    progress_data.pop(task_id, None)
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass

    if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        print(f"[DOWNLOAD] ffmpeg succeeded")
        return True, ""

    ffmpeg_err = stderr_bytes.decode(errors="replace").strip()
    last_lines = "\n".join(ffmpeg_err.split("\n")[-3:])
    print(f"[DOWNLOAD] ffmpeg also failed: {last_lines}")
    return False, f"yt-dlp error:\n{ytdlp_err[-300:]}\n\nffmpeg error:\n{last_lines}"


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
        "/test <url> - Test URL from server with curl\n"
        "/update - Pull from GitHub and restart"
    )


# ---------------------------------------------------------------------------
# /test command  (debug URL accessibility from server)
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("test"))
async def handle_test(client, message):
    """Run curl from the server to debug URL accessibility."""
    url = extract_url(message.text)
    if not url:
        await message.reply_text("Send /test <url>")
        return

    status = await message.reply_text("Testing URL from server...")

    # Run curl with verbose headers
    curl_cmd = [
        "curl", "-sS", "-o", "/dev/null",
        "-w", "HTTP %{http_code}\nSize: %{size_download} bytes\nTime: %{time_total}s\nIP: %{remote_ip}",
        "-H", f"User-Agent: {HEADERS['User-Agent']}",
        "-H", f"Referer: {HEADERS['Referer']}",
        "-H", f"Origin: {HEADERS['Origin']}",
        "--max-time", "15",
        url,
    ]

    result = await asyncio.create_subprocess_exec(
        *curl_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await result.communicate()
    output = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    # Also test without custom headers
    curl_cmd_plain = [
        "curl", "-sS", "-o", "/dev/null",
        "-w", "HTTP %{http_code}",
        "--max-time", "15",
        url,
    ]
    result2 = await asyncio.create_subprocess_exec(
        *curl_cmd_plain,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout2, _ = await result2.communicate()
    plain_status = stdout2.decode(errors="replace").strip()

    text = f"Curl test results:\n\nWith headers:\n{output}\n"
    if err:
        text += f"\nErrors: {err}\n"
    text += f"\nWithout headers:\n{plain_status}"

    await status.edit_text(text)


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


@app.on_message(owner_filter & filters.text & ~filters.command(["update", "start", "test", "help"]))
async def handle_link(client, message):
    url = extract_url(message.text)
    if url is None:
        return

    if not is_video_url(url):
        return

    # Check token expiry before wasting time
    expired, expiry_str = check_token_expiry(url)
    print(f"[LINK] Received URL, expired={expired}, expiry={expiry_str}")

    if expired:
        await message.reply_text(
            f"Link expired\n\nExpired on: {expiry_str}\n\n"
            "Send a fresh link with a valid token."
        )
        return

    if expiry_str:
        print(f"[LINK] Token valid until: {expiry_str}")

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
        success, error_text = await _download_video(url, output_path, task_id)

        if not success:
            updater.cancel()
            # Truncate error to fit Telegram message limit
            if len(error_text) > 3500:
                error_text = error_text[:3500] + "..."
            await status_msg.edit_text(f"Download failed\n\n{error_text}")
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
