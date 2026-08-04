import os
import sys
import subprocess
import shutil

# ---------------------------------------------------------------------------
# Auto-install missing dependencies
# ---------------------------------------------------------------------------

def _ensure_deps():
    """Install missing Python packages and check CLI tools at startup."""
    packages = {
        "pyrogram": "pyrogram",
        "tgcrypto": "tgcrypto",
        "dotenv": "python-dotenv",
        "yt_dlp": "yt-dlp",
        "Crypto": "pycryptodome",
    }
    missing = []
    for import_name, pip_name in packages.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print(f"[SETUP] Installing: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        )
        print(f"[SETUP] Installed successfully")

    # Check CLI tools
    if not shutil.which("ffmpeg"):
        print("[SETUP] WARNING: ffmpeg not found in PATH. Install it:")
        print("  Ubuntu/Debian: sudo apt install ffmpeg")
        print("  CentOS/RHEL:   sudo yum install ffmpeg")

    print("[SETUP] Ensuring yt-dlp is up-to-date...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"]
        )
    except Exception as e:
        print(f"[SETUP] yt-dlp check warning: {e}")

_ensure_deps()

# ---------------------------------------------------------------------------
# Imports (safe to import now, deps are installed)
# ---------------------------------------------------------------------------

import asyncio
import re
import time
import base64
import gzip
from io import BytesIO
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from dotenv import load_dotenv
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import math
import random
from pyrogram.client import Client
from pyrogram.raw.types import InputFileBig, InputFile
from pyrogram.raw.functions.upload import SaveBigFilePart, SaveFilePart
from typing import Union

# ---------------------------------------------------------------------------
# Monkey Patch Pyrogram Fast Uploader
# ---------------------------------------------------------------------------

original_save_file = Client.save_file

async def fast_save_file(self: Client, *args, **kwargs) -> Union[InputFile, InputFileBig]:
    path = kwargs.get("path")
    if path is None and len(args) > 0:
        path = args[0]
        
    # Fallback to original save_file for small files or if path is missing
    if not path or not isinstance(path, (str, Path)) or not os.path.exists(path):
        return await original_save_file(self, *args, **kwargs)
        
    file_size = os.path.getsize(path)
    if file_size < 10 * 1024 * 1024:
        return await original_save_file(self, *args, **kwargs)
        
    # Extract other arguments if passed
    file_id = kwargs.get("file_id")
    file_part = kwargs.get("file_part", 0)
    progress = kwargs.get("progress")
    progress_args = kwargs.get("progress_args", ())
    
    part_size = 512 * 1024
    total_parts = math.ceil(file_size / part_size)
    if file_id is None:
        file_id = random.getrandbits(63)
        
    sem = asyncio.Semaphore(10)  # Reduced from 50 to prevent Telegram rate limits
    uploaded_bytes = 0
    
    task_id = progress_args[0] if progress_args else None
    
    async def upload_part(part_index):
        if task_id in cancelled_tasks:
            raise Exception("Cancelled by user")
            
        nonlocal uploaded_bytes
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
            
            for attempt in range(10):  # 10 retries per chunk
                try:
                    await self.invoke(rpc)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    await asyncio.sleep(3)
            else:
                raise Exception(f"Failed to upload part {part_index}")
                
            uploaded_bytes += len(chunk)
            if progress:
                if asyncio.iscoroutinefunction(progress):
                    await progress(uploaded_bytes, file_size, *progress_args)
                else:
                    progress(uploaded_bytes, file_size, *progress_args)

    # Launch all chunk upload tasks concurrently
    tasks = [asyncio.create_task(upload_part(i)) for i in range(total_parts)]
    await asyncio.gather(*tasks)

    return InputFileBig(
        id=file_id,
        parts=total_parts,
        name=Path(path).name
    )

# Inject the fast uploader into Pyrogram's Client
Client.save_file = fast_save_file

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

# Clean up stale downloads from previous runs (e.g., if interrupted by /update)
import shutil
if DOWNLOAD_DIR.exists():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
    ),
    "Referer": "https://appx-play.akamai.net.in/",
    "Origin": "https://appx-play.akamai.net.in",
    "x-requested-with": "mark.via.gp"
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
cancelled_tasks = set()
active_processes = {}

# ---------------------------------------------------------------------------
# Custom headers (loaded from headers.json, set via /setheaders)
# ---------------------------------------------------------------------------

import json

HEADERS_FILE = BASE_DIR / "headers.json"
custom_headers = {}

def _load_custom_headers():
    global custom_headers
    if HEADERS_FILE.exists():
        try:
            custom_headers = json.loads(HEADERS_FILE.read_text())
            print(f"[SETUP] Loaded {len(custom_headers)} custom headers")
        except Exception:
            custom_headers = {}

def _save_custom_headers():
    HEADERS_FILE.write_text(json.dumps(custom_headers, indent=2))

def _get_all_headers():
    """Merge default HEADERS with custom_headers. Custom headers override defaults."""
    merged = dict(HEADERS)
    merged.update(custom_headers)
    return merged

_load_custom_headers()

# ---------------------------------------------------------------------------
# API config (loaded from api_config.json, set via /setapi)
# ---------------------------------------------------------------------------

API_CONFIG_FILE = BASE_DIR / "api_config.json"
api_config = {
    "authorization": os.getenv("CLASSX_AUTH", ""),
    "user-id": os.getenv("CLASSX_USER_ID", ""),
    "x-device-id": os.getenv("CLASSX_DEVICE_ID", "20f953f4c295fe94"),
    "course-id": os.getenv("CLASSX_COURSE_ID", "130"),
}

def _load_api_config():
    global api_config
    if API_CONFIG_FILE.exists():
        try:
            api_config = json.loads(API_CONFIG_FILE.read_text())
            print(f"[SETUP] Loaded API config (user-id: {api_config.get('user-id', 'N/A')})")
        except Exception:
            api_config = {}

def _save_api_config():
    API_CONFIG_FILE.write_text(json.dumps(api_config, indent=2))

_load_api_config()

# ---------------------------------------------------------------------------
# ClassX API helpers
# ---------------------------------------------------------------------------

CLASSX_API_BASE = "https://yodhaappapi.classx.co.in"

def _get_api_headers():
    """Build headers for ClassX API calls."""
    return {
        "Host": "yodhaappapi.classx.co.in",
        "client-service": "Appx",
        "auth-key": "appxapi",
        "user-id": api_config.get("user-id", ""),
        "authorization": api_config.get("authorization", ""),
        "user-app-category": "",
        "language": "en",
        "x-tenant-app-version": "132",
        "device-type": "ANDROID",
        "x-device-id": api_config.get("x-device-id", "20f953f4c295fe94"),
        "accept-encoding": "gzip",
        "user-agent": "okhttp/5.3.2",
    }

def _api_get(url):
    """Make a GET request to ClassX API and return JSON."""
    headers = _get_api_headers()
    req = Request(url)
    for k, v in headers.items():
        req.add_header(k, v)

    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
        # Handle gzip response
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

def _get_folder_contents(course_id, parent_id):
    """Get all items in a folder."""
    url = (f"{CLASSX_API_BASE}/get/folder_contentsv3"
           f"?start=0&course_id={course_id}&parent_id={parent_id}")
    return _api_get(url)

def _get_video_details(course_id, video_id):
    """Get full video details including encrypted links."""
    url = (f"{CLASSX_API_BASE}/get/fetchVideoDetailsById"
           f"?course_id={course_id}&folder_wise_course=1&ytflag=0&video_id={video_id}")
    return _api_get(url)

def _decrypt_classx_url(encrypted_str):
    """Decrypt ClassX encrypted video link if encrypted."""
    if not encrypted_str:
        return ""
    if encrypted_str.startswith("http"):
        return encrypted_str
    try:
        key = b"6385731993184651"
        iv = b"6385731993184651"
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(base64.b64decode(encrypted_str))
        decrypted = unpad(decrypted, AES.block_size).decode("utf-8")
        if decrypted.startswith("http"):
            return decrypted
    except Exception:
        pass
    return ""

def _resolve_video_url(item_data, course_id):
    """Extract or fetch video URL for a given item.

    Returns (url: str, error_msg: str).
    """
    direct_fields = [
        "download_link", "video_link", "link", "url", "hls_url", "m3u8_url",
        "encrypted_link", "encrypted_link_360", "encrypted_link_480", "encrypted_link_720", "encrypted_link_1080"
    ]

    # 1. Check direct fields on item
    for field in direct_fields:
        val = item_data.get(field, "")
        if not val:
            continue
        if str(val).startswith("http"):
            return str(val), ""
        dec = _decrypt_classx_url(str(val))
        if dec:
            return dec, ""

    # 2. Fetch video details via API if ID exists
    video_id = item_data.get("id") or item_data.get("video_id")
    if not video_id:
        keys_found = list(item_data.keys())
        return "", f"No video ID in item (keys: {keys_found[:5]})"

    try:
        details = _get_video_details(course_id, video_id)
        if not isinstance(details, dict):
            return "", f"Invalid API response type: {type(details)}"

        status = details.get("status")
        msg = details.get("message", "")
        if status != 200:
            return "", f"API Error status {status}: {msg}"

        ddata = details.get("data", {})
        if isinstance(ddata, list) and ddata:
            ddata = ddata[0]

        if isinstance(ddata, dict):
            for field in direct_fields:
                val = ddata.get(field, "")
                if not val:
                    continue
                if str(val).startswith("http"):
                    return str(val), ""
                dec = _decrypt_classx_url(str(val))
                if dec:
                    return dec, ""

            avail_keys = list(ddata.keys())
            return "", f"No video link in API response (keys: {avail_keys[:8]})"
        else:
            return "", f"API data empty or invalid: {type(ddata)}"

    except HTTPError as e:
        return "", f"HTTP {e.code}: {e.reason}"
    except URLError as e:
        return "", f"URL Error: {e.reason}"
    except Exception as e:
        return "", f"API Exception: {str(e)}"

def _resolve_pdf_urls(item_data):
    """Extract PDF URLs from item data."""
    pdfs = []
    
    # ClassX can have multiple PDFs (study_material_link, pdf_link, pdf_link2, pdf_link3, etc.)
    pdf_fields = ["study_material_link", "pdf_link"] + [f"pdf_link{i}" for i in range(2, 11)]
    
    for field in pdf_fields:
        link = item_data.get(field, "")
        if not link:
            continue
        if link.startswith("http"):
            pdfs.append(link)
    return pdfs

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


def is_supported_url(url):
    """Check whether the URL looks like a supported video or document link."""
    keywords = ["m3u8", "video", "stream", "mp4", "hls", ".ts", ".pdf"]
    lower = url.lower()
    return any(kw in lower for kw in keywords)


def is_pdf_url(url):
    return ".pdf" in url.lower()


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
    expiry_str = expiry_dt.strftime("%I:%M %p %d/%m/%Y")

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
                btn = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=f"cancel_{task_id}")]])
                await status_msg.edit_text(text, reply_markup=btn)
                last_text = text
            except Exception:
                pass

        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Direct Downloader (For PDFs and standard files)
# ---------------------------------------------------------------------------

async def _download_direct(url, output_path, task_id):
    """Download a simple file natively without yt-dlp."""
    all_headers = _get_all_headers()
    
    def do_download():
        req = Request(url)
        for k, v in all_headers.items():
            req.add_header(k, v)
            
        try:
            with urlopen(req, timeout=30) as response, open(output_path, 'wb') as out_file:
                while True:
                    if task_id in cancelled_tasks:
                        return False, "Cancelled by user"
                    if task_id not in progress_data:
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


# ---------------------------------------------------------------------------
# Video downloader (yt-dlp primary, ffmpeg fallback)
# ---------------------------------------------------------------------------


async def _download_video(url, output_path, task_id):
    """Download HLS video. Tries yt-dlp first, falls back to ffmpeg.

    Returns (success: bool, error_msg: str).
    """
    # Merge default + custom headers
    all_headers = _get_all_headers()
    ffmpeg_headers = "".join(f"{k}: {v}\r\n" for k, v in all_headers.items())

    # File size monitor (shared by both yt-dlp and ffmpeg)
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

    # --- Try yt-dlp first ---
    ytdlp_err = "yt-dlp not installed"
    try:
        ytdlp_cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-check-certificates",
            "-N", "16",
            "--concurrent-fragments", "16",
            "--hls-use-mpegts",
            "--fragment-retries", "10",
            "--retries", "10",
            "--file-access-retries", "10",
            "--buffer-size", "1M",
            "--http-chunk-size", "10M",
        ]

        if shutil.which("aria2c"):
            ytdlp_cmd += [
                "--downloader", "aria2c",
                "--downloader-args", "aria2c:-j 16 -s 16 -x 16 -k 1M"
            ]

        for hk, hv in all_headers.items():
            ytdlp_cmd += ["--add-header", f"{hk}: {hv}"]
        ytdlp_cmd += ["-o", output_path, url]

        print(f"[DOWNLOAD] Trying yt-dlp...")
        process = await asyncio.create_subprocess_exec(
            *ytdlp_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        active_processes[task_id] = process
        _, stderr_bytes = await process.communicate()
        active_processes.pop(task_id, None)
        
        if task_id in cancelled_tasks:
            return False, "Cancelled by user"

        if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            monitor.cancel()
            print(f"[DOWNLOAD] yt-dlp succeeded")
            return True, ""

        ytdlp_err = stderr_bytes.decode(errors="replace").strip()
        print(f"[DOWNLOAD] yt-dlp failed: {ytdlp_err[-200:]}")

    except FileNotFoundError:
        print(f"[DOWNLOAD] yt-dlp not installed, skipping")

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
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
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
    active_processes[task_id] = process
    _, stderr_bytes = await process.communicate()
    active_processes.pop(task_id, None)

    if task_id in cancelled_tasks:
        return False, "Cancelled by user"

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
    return False, f"yt-dlp: {ytdlp_err[-300:]}\n\nffmpeg: {last_lines}"


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
        "Bot is running...\n\n"
        "/start - Show this message\n"
        "/batch - Batch download chapter\n"
        "/api - Show API config\n"
        "/setapi - Set ClassX API credentials\n"
        "/update - Pull from GitHub and restart"
    )


# ---------------------------------------------------------------------------
# /setapi, /api, /clearapi
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("setapi"))
async def handle_setapi(client, message):
    """Set ClassX API credentials.

    Usage: /setapi
    authorization: eyJ0eXAi...
    user-id: 154346
    x-device-id: 20f953f4c295fe94
    """
    global api_config
    lines = message.text.split("\n")[1:]
    if not lines:
        await message.reply_text(
            "Usage:\n\n/setapi\n"
            "authorization: eyJ0eXAi...\n"
            "user-id: 154346\n"
            "x-device-id: 20f953f4c295fe94"
        )
        return

    parsed = {}
    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()

    if not parsed:
        await message.reply_text("No valid config found...")
        return

    api_config = parsed
    _save_api_config()

    display = "\n".join(
        f"{k}: {v[:40]}..." if len(v) > 40 else f"{k}: {v}"
        for k, v in parsed.items()
    )
    await message.reply_text(f"API config saved...\n\n{display}")


@app.on_message(owner_filter & filters.command("api"))
async def handle_api(client, message):
    if not api_config:
        await message.reply_text("API config not set... Use /setapi")
        return

    display = "\n".join(
        f"{k}: {v[:50]}..." if len(v) > 50 else f"{k}: {v}"
        for k, v in api_config.items()
    )
    await message.reply_text(f"API Config:\n\n{display}")


@app.on_message(owner_filter & filters.command("clearapi"))
async def handle_clearapi(client, message):
    global api_config
    api_config = {}
    if API_CONFIG_FILE.exists():
        API_CONFIG_FILE.unlink()
    await message.reply_text("API config cleared...")


# ---------------------------------------------------------------------------
# /batch command - Auto download entire chapter
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("batch"))
async def handle_batch(client, message):
    """Batch download all videos and PDFs from a chapter.

    Usage: /batch <parent_id>
    Or:    /batch <course_id> <parent_id>
    """
    if not api_config.get("authorization"):
        await message.reply_text("API not configured... Use /setapi first")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply_text("Usage: /batch <parent_id>")
        return

    if len(parts) >= 3:
        course_id = parts[1]
        parent_id = parts[2]
    else:
        course_id = api_config.get("course-id", "130")
        parent_id = parts[1]

    status_msg = await message.reply_text("Fetching chapter contents...")

    # Step 1: Get folder contents
    try:
        folder_data = await asyncio.to_thread(_get_folder_contents, course_id, parent_id)
    except Exception as e:
        await status_msg.edit_text(f"API error...\n\n{e}")
        return

    if folder_data.get("status") != 200:
        await status_msg.edit_text(f"API error...\n\n{folder_data.get('message', 'Unknown')}")
        return

    items = folder_data.get("data", [])
    if not items:
        await status_msg.edit_text("No items found in this chapter...")
        return

    total = len(items)
    video_count = sum(1 for i in items if i.get("material_type") == "VIDEO")
    
    # Count total PDFs accurately
    pdf_fields = ["study_material_link", "pdf_link"] + [f"pdf_link{i}" for i in range(2, 11)]
    pdf_count = 0
    for i in items:
        for field in pdf_fields:
            if i.get(field):
                pdf_count += 1

    await status_msg.edit_text(
        f"Found {total} items...\n"
        f"Videos: {video_count}\n"
        f"PDFs: {pdf_count}\n\n"
        f"Processing started..."
    )
    await asyncio.sleep(2)

    # Step 2: Process each item
    success_count = 0
    fail_count = 0
    failed_reasons = []

    for idx, item in enumerate(items, 1):
        title = item.get("Title") or item.get("title") or f"Item {idx}"
        item_id = item.get("id") or item.get("video_id")
        material_type = item.get("material_type") or item.get("type", "")

        try:
            await status_msg.edit_text(f"Processing {idx}/{total}...\n\nTitle: {title}")
        except Exception:
            pass

        # --- Handle PDFs ---
        pdf_urls = _resolve_pdf_urls(item)
        if pdf_urls:
            try:
                await status_msg.edit_text(f"[{idx}/{total}] {title} - Found {len(pdf_urls)} PDFs")
            except Exception:
                pass
            await asyncio.sleep(2)

        for pidx, pdf_url in enumerate(pdf_urls):
            try:
                pdf_label = f"{title} - PDF{pidx + 1}" if len(pdf_urls) > 1 else f"{title} - PDF"
                task_id = f"batch_pdf_{item_id}_{pidx}_{int(time.time())}"
                pdf_filename = f"{pdf_label}.pdf".replace("/", "-").replace("\\", "-")
                pdf_path = str(DOWNLOAD_DIR / pdf_filename)

                progress_data[task_id] = {
                    "phase": f"Downloading...\n{pdf_label}",
                    "current": 0,
                    "total": 0,
                }

                updater = asyncio.create_task(_progress_loop(status_msg, task_id))
                success, err = await _download_direct(pdf_url, pdf_path, task_id)

                if not success or not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                    progress_data.pop(task_id, None)
                    updater.cancel()
                    fail_count += 1
                    failed_reasons.append(f"PDF '{pdf_label}': Download failed - {err[:150]}")
                    try:
                        await status_msg.edit_text(f"PDF download failed for {pdf_label}:\n{err}")
                        await asyncio.sleep(3)
                    except Exception:
                        pass
                    continue

                file_size = os.path.getsize(pdf_path)
                progress_data[task_id] = {
                    "phase": f"Uploading...\n{pdf_label}",
                    "current": 0,
                    "total": file_size,
                }

                await client.send_document(
                    chat_id=message.chat.id,
                    document=pdf_path,
                    file_name=pdf_filename,
                    caption=pdf_label,
                    progress=_upload_progress,
                    progress_args=(task_id,),
                )

                progress_data.pop(task_id, None)
                updater.cancel()
                success_count += 1

            except Exception as e:
                fail_count += 1
                failed_reasons.append(f"PDF '{title}': Error - {str(e)[:150]}")
                try:
                    await status_msg.edit_text(f"Error processing PDF {pdf_label}:\n{e}")
                    await asyncio.sleep(3)
                except Exception:
                    pass
            finally:
                try:
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)
                except OSError:
                    pass

        # --- Handle Videos ---
        is_video_item = (material_type == "VIDEO") or bool(item.get("video_id")) or (not pdf_urls and material_type != "PDF")
        if is_video_item:
            video_url, resolve_err = await asyncio.to_thread(_resolve_video_url, item, course_id)
            if not video_url:
                print(f"[BATCH] No video URL found for item {item_id}: {title} ({resolve_err})")
                if not pdf_urls:
                    fail_count += 1
                    failed_reasons.append(f"Video '{title}': {resolve_err}")
                continue

            try:
                vid_label = f"{title}"
                task_id = f"batch_vid_{item_id}_{int(time.time())}"
                vid_filename = f"{vid_label}.mp4".replace("/", "-").replace("\\", "-").replace(":", "-").replace("*", "")
                vid_path = str(DOWNLOAD_DIR / vid_filename)

                progress_data[task_id] = {
                    "phase": f"Downloading Video...\n{vid_label}",
                    "current": 0,
                    "total": 0,
                }

                updater = asyncio.create_task(_progress_loop(status_msg, task_id))
                success, err = await _download_video(video_url, vid_path, task_id)

                if not success or not os.path.exists(vid_path) or os.path.getsize(vid_path) == 0:
                    progress_data.pop(task_id, None)
                    updater.cancel()
                    fail_count += 1
                    failed_reasons.append(f"Video '{title}': Download failed - {err[:150]}")
                    try:
                        await status_msg.edit_text(f"Video download failed for {vid_label}:\n{err[:300]}")
                        await asyncio.sleep(3)
                    except Exception:
                        pass
                    continue

                file_size = os.path.getsize(vid_path)
                progress_data[task_id] = {
                    "phase": f"Uploading Video...\n{vid_label}",
                    "current": 0,
                    "total": file_size,
                }

                await client.send_video(
                    chat_id=message.chat.id,
                    video=vid_path,
                    file_name=vid_filename,
                    caption=vid_label,
                    supports_streaming=True,
                    progress=_upload_progress,
                    progress_args=(task_id,),
                )

                progress_data.pop(task_id, None)
                updater.cancel()
                success_count += 1

            except Exception as e:
                fail_count += 1
                failed_reasons.append(f"Video '{title}': Exception - {str(e)[:150]}")
                try:
                    await status_msg.edit_text(f"Error processing video {vid_label}:\n{e}")
                    await asyncio.sleep(3)
                except Exception:
                    pass
            finally:
                try:
                    if os.path.exists(vid_path):
                        os.remove(vid_path)
                except OSError:
                    pass

    # Final summary
    summary = (
        f"Batch complete...\n\n"
        f"Total: {total}\n"
        f"Done: {success_count}\n"
        f"Failed: {fail_count}"
    )
    if failed_reasons:
        summary += "\n\nFailures detail:\n" + "\n".join(f"• {r}" for r in failed_reasons)

    if len(summary) > 4000:
        summary = summary[:3900] + "\n\n...[truncated]"

    try:
        await status_msg.edit_text(summary)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Callback Query Handler (for cancellation)
# ---------------------------------------------------------------------------

@app.on_callback_query(filters.regex(r"^cancel_"))
async def handle_cancel_task(client, callback_query: CallbackQuery):
    task_id = callback_query.data.split("cancel_", 1)[1]
    cancelled_tasks.add(task_id)
    
    # Kill subprocess if any
    proc = active_processes.get(task_id)
    if proc:
        try:
            proc.kill()
        except OSError:
            pass
            
    await callback_query.answer()
    try:
        msg_text = callback_query.message.text or "Task"
        await callback_query.message.edit_text(f"{msg_text}\n\nTask Cancelled")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /setheaders, /headers, /clearheaders
# ---------------------------------------------------------------------------


@app.on_message(owner_filter & filters.command("setheaders"))
async def handle_setheaders(client, message):
    """Set custom headers from message text.

    Usage: /setheaders
    Header1: value1
    Header2: value2
    """
    global custom_headers
    lines = message.text.split("\n")[1:]  # Skip the /setheaders line
    if not lines:
        await message.reply_text(
            "Usage:\n\n/setheaders\n"
            "Cookie: your_cookie_here\n"
            "Authorization: Bearer token\n\n"
            "Paste headers from 1DM download details (one per line)."
        )
        return

    parsed = {}
    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()

    if not parsed:
        await message.reply_text("No valid headers found. Format: Key: Value")
        return

    custom_headers = parsed
    _save_custom_headers()

    header_list = "\n".join(f"{k}: {v[:50]}..." if len(v) > 50 else f"{k}: {v}" for k, v in parsed.items())
    await message.reply_text(f"Saved {len(parsed)} custom headers:\n\n{header_list}")


@app.on_message(owner_filter & filters.command("headers"))
async def handle_headers(client, message):
    all_h = _get_all_headers()
    if not all_h:
        await message.reply_text("No headers set.")
        return

    lines = []
    for k, v in all_h.items():
        tag = " [custom]" if k in custom_headers else " [default]"
        display_v = v[:60] + "..." if len(v) > 60 else v
        lines.append(f"{k}: {display_v}{tag}")

    await message.reply_text("Current headers:\n\n" + "\n".join(lines))


@app.on_message(owner_filter & filters.command("clearheaders"))
async def handle_clearheaders(client, message):
    global custom_headers
    custom_headers = {}
    if HEADERS_FILE.exists():
        HEADERS_FILE.unlink()
    await message.reply_text("Custom headers cleared. Only default headers remain.")


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

    # Test 1: curl with all headers (default + custom), show response body
    all_h = _get_all_headers()
    curl_cmd = [
        "curl", "-sS",
        "-w", "\n---\nHTTP %{http_code} | Size: %{size_download}B | IP: %{remote_ip}",
    ]
    for hk, hv in all_h.items():
        curl_cmd += ["-H", f"{hk}: {hv}"]
    curl_cmd += ["--max-time", "15", url]

    result = await asyncio.create_subprocess_exec(
        *curl_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await result.communicate()
    body_and_stats = stdout.decode(errors="replace").strip()

    # Test 2: try URL without &bitrate= parameter
    clean_url = re.sub(r"[&?]bitrate=\d+", "", url)
    custom_tag = f"\nCustom headers: {len(custom_headers)}" if custom_headers else ""
    alt_result = ""
    if clean_url != url:
        curl_cmd2 = [
            "curl", "-sS",
            "-w", "\n---\nHTTP %{http_code} | Size: %{size_download}B",
            "-H", f"User-Agent: {HEADERS['User-Agent']}",
            "-H", f"Referer: {HEADERS['Referer']}",
            "-H", f"Origin: {HEADERS['Origin']}",
            "--max-time", "15",
            clean_url,
        ]
        r2 = await asyncio.create_subprocess_exec(
            *curl_cmd2,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        s2, _ = await r2.communicate()
        alt_result = f"\n\nWithout bitrate param:\n{s2.decode(errors='replace').strip()}"

    text = f"Full URL:{custom_tag}\n{body_and_stats}{alt_result}"
    if len(text) > 4000:
        text = text[:4000] + "..."

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


@app.on_message(owner_filter & filters.text & ~filters.command(["update", "start", "test", "setheaders", "headers", "clearheaders", "setapi", "api", "clearapi", "batch", "help"]))
async def handle_link(client, message):
    url = extract_url(message.text)
    if url is None:
        return

    if not is_supported_url(url):
        return

    is_pdf = is_pdf_url(url)

    # Check token expiry before wasting time
    expired, expiry_str = check_token_expiry(url)
    print(f"[LINK] Received URL, expired={expired}, expiry={expiry_str}")

    if expired:
        await message.reply_text(
            f"Link Expired...\n\n{expiry_str}\n\n"
            "Send a fresh link..."
        )
        return

    if expiry_str:
        print(f"[LINK] Token valid until: {expiry_str}")

    task_id = f"{message.chat.id}_{message.id}_{int(time.time())}"
    if is_pdf:
        filename = f"document_{message.id}_{int(time.time())}.pdf"
    else:
        filename = f"video_{message.id}_{int(time.time())}.mp4"
    output_path = str(DOWNLOAD_DIR / filename)

    # -- Download phase ----------------------------------------------------

    status_msg = await message.reply_text("Downloading...\n\n0 B")

    progress_data[task_id] = {
        "phase": "Downloading...",
        "current": 0,
        "total": 0,
    }

    updater = asyncio.create_task(_progress_loop(status_msg, task_id))

    try:
        if is_pdf:
            success, error_text = await _download_direct(url, output_path, task_id)
        else:
            success, error_text = await _download_video(url, output_path, task_id)

        if not success:
            updater.cancel()
            # Truncate error to fit Telegram message limit
            if len(error_text) > 3500:
                error_text = error_text[:3500] + "..."
            await status_msg.edit_text(f"Download failed...\n\n{error_text}")
            return

        file_size = os.path.getsize(output_path)

        if file_size == 0:
            updater.cancel()
            await status_msg.edit_text("Download failed: file is empty...")
            return

        # -- Upload phase --------------------------------------------------

        progress_data[task_id] = {
            "phase": "Uploading...",
            "current": 0,
            "total": file_size,
        }

        # The updater task is still running; it will now pick up the new phase.

        if is_pdf:
            await client.send_document(
                chat_id=message.chat.id,
                document=output_path,
                file_name=filename,
                progress=_upload_progress,
                progress_args=(task_id,),
            )
        else:
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

        # Delete the status message as requested by user
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as exc:
        progress_data.pop(task_id, None)
        updater.cancel()
        await status_msg.edit_text(f"Error...\n\n{exc}")

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
