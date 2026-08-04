"""
Telegram Bot for Downloading Direct Videos (.m3u8 / .mp4) and PDFs (.pdf)
and uploading to Telegram Group.
"""

import os
import sys
import re
import time
import json
import math
import random
import shutil
import asyncio
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

def _ensure_deps():
    print("[SETUP] Ensuring yt-dlp is up-to-date...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"]
        )
    except Exception as e:
        print(f"[SETUP] yt-dlp check warning: {e}")

_ensure_deps()

from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID"))
OWNER_GROUP = int(os.getenv("OWNER_GROUP"))

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
RESTART_FILE = BASE_DIR / "restart_info.json"

if DOWNLOAD_DIR.exists():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER = "https://player.akamai.net.in/"

app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(BASE_DIR),
)

progress_data = {}
cancelled_tasks = set()
active_processes = {}

# Custom fast Pyrogram uploader
from pyrogram.raw.functions.messages import SaveBigFilePart

async def fast_upload_file(client, path, progress=None, progress_args=()):
    file_size = os.path.getsize(path)
    part_size = 512 * 1024
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    
    sem = asyncio.Semaphore(10)
    uploaded_bytes = 0
    task_id = progress_args[0] if progress_args else None
    
    async def upload_part(part_index):
        nonlocal uploaded_bytes
        if task_id in cancelled_tasks:
            raise Exception("Cancelled by user")
        async with sem:
            if task_id in cancelled_tasks:
                raise Exception("Cancelled by user")
            with open(path, "rb") as f:
                f.seek(part_index * part_size)
                chunk = f.read(part_size)
            
            rpc = SaveBigFilePart(
                file_id=file_id,
                file_part=part_index,
                file_total_parts=total_parts,
                bytes=chunk
            )
            await client.invoke(rpc)
            uploaded_bytes += len(chunk)
            if progress:
                await progress(uploaded_bytes, file_size, *progress_args)

    tasks = [upload_part(i) for i in range(total_parts)]
    await asyncio.gather(*tasks)
    return await client.save_file(path, file_id=file_id)

client_send_document_orig = Client.send_document
client_send_video_orig = Client.send_video

async def send_document_fast(self, chat_id, document, **kwargs):
    if "progress" in kwargs and os.path.exists(str(document)):
        try:
            input_file = await fast_upload_file(
                self, str(document),
                progress=kwargs.get("progress"),
                progress_args=kwargs.get("progress_args", ())
            )
            kwargs["document"] = input_file
        except Exception as e:
            print(f"[FAST_UPLOAD] Fallback to standard upload: {e}")
    return await client_send_document_orig(self, chat_id, **kwargs)

async def send_video_fast(self, chat_id, video, **kwargs):
    if "progress" in kwargs and os.path.exists(str(video)):
        try:
            thumb = kwargs.get("thumb")
            if thumb and isinstance(thumb, str) and os.path.exists(thumb):
                try:
                    kwargs["thumb"] = await self.save_file(thumb)
                except Exception as e:
                    print(f"[FAST_UPLOAD] Thumb upload fallback: {e}")

            input_file = await fast_upload_file(
                self, str(video),
                progress=kwargs.get("progress"),
                progress_args=kwargs.get("progress_args", ())
            )
            kwargs["video"] = input_file
        except Exception as e:
            print(f"[FAST_UPLOAD] Fallback to standard upload: {e}")
    return await client_send_video_orig(self, chat_id, **kwargs)

Client.send_document = send_document_fast
Client.send_video = send_video_fast

def _owner_group_check(_, __, message):
    if not message or not message.chat:
        return False
    cid = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    txt = (message.text or message.caption or "")[:40]
    
    print(f"[RECV] Message in chat {cid} from user {user_id}: '{txt}'")
    is_allowed = (cid == OWNER_GROUP) or (OWNER_ID and cid == OWNER_ID) or (user_id and user_id == OWNER_ID)
    if is_allowed:
        print(f"[ALLOW] Authorized message from chat {cid}")
    else:
        print(f"[DENY] Unauthorized message from chat {cid} (Target OWNER_GROUP={OWNER_GROUP}, OWNER_ID={OWNER_ID})")
    return is_allowed

owner_filter = filters.create(_owner_group_check)

def format_bytes(size):
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    size = float(size)
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"

def extract_url(message):
    text = (message.text or message.caption or "") if message else ""
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None

def is_pdf_url(url):
    return ".pdf" in url.lower() or "pdf" in url.lower()

def _prepare_video_metadata_and_faststart(video_path):
    """
    1. Apply MP4 faststart (-movflags +faststart) so Telegram streams video progressively while downloading.
    2. Extract duration (s), width (px), height (px).
    3. Generate JPG cover thumbnail.
    """
    faststart_path = video_path
    try:
        tmp_fast = str(Path(video_path).parent / f"fast_{Path(video_path).name}")
        fs_cmd = ["ffmpeg", "-y", "-i", video_path, "-c", "copy", "-movflags", "+faststart", tmp_fast]
        res = subprocess.run(fs_cmd, capture_output=True, timeout=120)
        if res.returncode == 0 and os.path.exists(tmp_fast) and os.path.getsize(tmp_fast) > 0:
            faststart_path = tmp_fast
    except Exception as e:
        print(f"[FASTSTART] Exception: {e}")

    duration = 0
    width = 1280
    height = 720
    thumb_path = None

    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "json",
            faststart_path
        ]
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams:
                width = int(streams[0].get("width", 1280))
                height = int(streams[0].get("height", 720))
                if streams[0].get("duration"):
                    duration = int(float(streams[0].get("duration")))
            if not duration:
                fmt = data.get("format", {})
                if fmt.get("duration"):
                    duration = int(float(fmt.get("duration")))
    except Exception as e:
        print(f"[PROBE] Exception: {e}")

    try:
        tmp_thumb = str(Path(video_path).parent / f"{Path(video_path).stem}_thumb.jpg")
        t_ss = "00:00:02" if duration >= 3 else "00:00:00"
        thumb_cmd = [
            "ffmpeg", "-y",
            "-ss", t_ss,
            "-i", faststart_path,
            "-vframes", "1",
            "-vf", "scale=320:-1",
            tmp_thumb
        ]
        res = subprocess.run(thumb_cmd, capture_output=True, timeout=10)
        if os.path.exists(tmp_thumb) and os.path.getsize(tmp_thumb) > 0:
            thumb_path = tmp_thumb
    except Exception as e:
        print(f"[THUMB] Exception: {e}")

    return faststart_path, duration, width, height, thumb_path

async def _progress_loop(status_msg, task_id):
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
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cancel_{task_id}")]])
                await status_msg.edit_text(text, reply_markup=btn)
                last_text = text
            except Exception:
                pass
        await asyncio.sleep(3)

async def _upload_progress(current, total, task_id):
    if task_id in cancelled_tasks:
        raise Exception("Cancelled by user")
    if task_id in progress_data:
        progress_data[task_id]["current"] = current
        progress_data[task_id]["total"] = total

async def _download_direct(url, output_path, task_id):
    def do_download():
        req = Request(url)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Referer", REFERER)
        try:
            with urlopen(req, timeout=30) as response, open(output_path, 'wb') as out_file:
                while True:
                    if task_id in cancelled_tasks or task_id not in progress_data:
                        return False, "Cancelled"
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    progress_data[task_id]["current"] = os.path.getsize(output_path)
            return True, ""
        except Exception as e:
            return False, str(e)
    return await asyncio.to_thread(do_download)

async def _download_video(url, output_path, task_id):
    async def _monitor():
        while task_id in progress_data:
            try:
                for p in [output_path] + list(DOWNLOAD_DIR.glob(f"{Path(output_path).stem}*")):
                    p = str(p)
                    if os.path.exists(p):
                        size = os.path.getsize(p)
                        if size > 0 and task_id in progress_data:
                            progress_data[task_id]["current"] = size
                            break
            except OSError:
                pass
            await asyncio.sleep(0.5)

    monitor = asyncio.create_task(_monitor())
    
    # Try yt-dlp first
    try:
        yt_bin = shutil.which("yt-dlp")
        ytdlp_base = [yt_bin] if yt_bin else [sys.executable, "-m", "yt_dlp"]
        ytdlp_cmd = ytdlp_base + [
            "--no-warnings",
            "--no-check-certificates",
            "--user-agent", USER_AGENT,
            "--referer", REFERER,
            "-N", "16",
            "--concurrent-fragments", "16",
            "--hls-use-mpegts",
            "--fragment-retries", "10",
            "--retries", "10",
            "--file-access-retries", "10",
            "--buffer-size", "1M",
            "--http-chunk-size", "10M",
            "-o", output_path,
            url
        ]
        if shutil.which("aria2c"):
            ytdlp_cmd += ["--downloader", "aria2c", "--downloader-args", "aria2c:-j 16 -s 16 -x 16 -k 1M"]

        process = await asyncio.create_subprocess_exec(
            *ytdlp_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        active_processes[task_id] = process
        _, stderr_bytes = await process.communicate()
        active_processes.pop(task_id, None)

        if task_id in cancelled_tasks:
            return False, "Cancelled by user"

        if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True, ""
    except Exception as e:
        print(f"[DOWNLOAD] yt-dlp exception: {e}")

    # Fallback to ffmpeg
    try:
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        ffmpeg_cmd = [
            ffmpeg_bin, "-y",
            "-headers", f"User-Agent: {USER_AGENT}\r\nReferer: {REFERER}\r\n",
            "-i", url,
            "-c", "copy",
            output_path
        ]
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        active_processes[task_id] = process
        _, stderr_bytes = await process.communicate()
        active_processes.pop(task_id, None)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True, ""
        return False, stderr_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)
    finally:
        monitor.cancel()

@app.on_callback_query()
async def handle_callback(client, callback):
    if callback.data.startswith("cancel_"):
        task_id = callback.data.replace("cancel_", "")
        cancelled_tasks.add(task_id)
        proc = active_processes.get(task_id)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        await callback.answer("Cancelled", show_alert=True)

@app.on_message(owner_filter & filters.command("start"))
async def handle_start(client, message):
    msg_text = (
        "Direct Video & PDF Downloader Bot is Active!\n\n"
        "How to use:\n"
        "• Send any direct video link (`.m3u8` / `.mp4`)\n"
        "• Send any direct PDF link (`.pdf`)\n\n"
        "Features:\n"
        "• High-speed 16-thread download (yt-dlp + aria2c)\n"
        "• Instant video streaming playback (faststart)\n"
        "• Auto cover thumbnail & video duration\n"
        "• Fast Pyrogram uploader\n\n"
        "Commands:\n"
        "/start - Show this menu\n"
        "/update - Pull latest code from GitHub & auto-restart\n"
        "/restart - Restart bot process"
    )
    await message.reply_text(msg_text)

@app.on_message(owner_filter & filters.command("update"))
async def handle_update(client, message):
    status = await message.reply_text("Pulling from GitHub...")
    result = subprocess.run(["git", "pull"], capture_output=True, text=True, cwd=str(BASE_DIR))
    output = (result.stdout.strip() or result.stderr.strip() or "No output")
    await status.edit_text(f"Update result:\n\n{output}\n\nRestarting...")
    restart_info = {"chat_id": message.chat.id, "output": output}
    RESTART_FILE.write_text(json.dumps(restart_info, indent=2))
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(owner_filter & filters.command("restart"))
async def handle_restart(client, message):
    status = await message.reply_text("Restarting bot process...")
    restart_info = {"chat_id": message.chat.id, "output": "Manual restart"}
    RESTART_FILE.write_text(json.dumps(restart_info, indent=2))
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(owner_filter & (filters.text | filters.caption) & ~filters.command(["update", "restart", "start"]))
async def handle_link(client, message):
    url = extract_url(message)
    if not url:
        print(f"[LINK] No URL found in message: '{message.text or message.caption}'")
        return

    print(f"[LINK] Processing URL: {url[:70]}...")
    is_pdf = is_pdf_url(url)
    task_id = f"{message.chat.id}_{message.id}_{int(time.time())}"
    filename = f"file_{message.id}_{int(time.time())}" + (".pdf" if is_pdf else ".mp4")
    output_path = str(DOWNLOAD_DIR / filename)

    status_msg = await message.reply_text("Downloading...\n\n0 B")
    progress_data[task_id] = {"phase": "Downloading...", "current": 0, "total": 0}
    updater = asyncio.create_task(_progress_loop(status_msg, task_id))

    try:
        if is_pdf:
            success, error_text = await _download_direct(url, output_path, task_id)
        else:
            success, error_text = await _download_video(url, output_path, task_id)

        if not success:
            updater.cancel()
            await status_msg.edit_text(f"Download failed...\n\n{error_text[:3500]}")
            return

        file_size = os.path.getsize(output_path)
        if file_size == 0:
            updater.cancel()
            await status_msg.edit_text("Download failed: file is empty...")
            return

        progress_data[task_id] = {"phase": "Uploading...", "current": 0, "total": file_size}

        if is_pdf:
            await client.send_document(
                chat_id=message.chat.id, document=output_path, file_name=filename,
                progress=_upload_progress, progress_args=(task_id,)
            )
        else:
            fast_video, duration, width, height, thumb_file = await asyncio.to_thread(
                _prepare_video_metadata_and_faststart, output_path
            )
            try:
                await client.send_video(
                    chat_id=message.chat.id,
                    video=fast_video,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb_file,
                    supports_streaming=True,
                    progress=_upload_progress,
                    progress_args=(task_id,)
                )
            finally:
                for f_path in [fast_video, thumb_file]:
                    if f_path and f_path != output_path and os.path.exists(f_path):
                        try:
                            os.remove(f_path)
                        except OSError:
                            pass

        progress_data.pop(task_id, None)
        updater.cancel()
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as exc:
        progress_data.pop(task_id, None)
        updater.cancel()
        await status_msg.edit_text(f"Error...\n\n{exc}")
    finally:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

@app.on_message(group=-1)
async def _check_restart_notification(client, message):
    if RESTART_FILE.exists():
        try:
            data = json.loads(RESTART_FILE.read_text())
            RESTART_FILE.unlink(missing_ok=True)
            chat_id = data.get("chat_id")
            out = data.get("output", "")
            msg = f"Restarted successfully!\n\nGit result:\n{out[:400]}"
            if chat_id:
                await client.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"[STARTUP] Restart error: {e}")
    message.continue_propagation()

if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("ERROR: BOT_TOKEN, API_ID, or API_HASH missing from .env")
        sys.exit(1)
    print("Bot started. Listening for direct video/PDF links...")
    app.run()
