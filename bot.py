import os
import sys
import shutil
import subprocess

# ---------------------------------------------------------------------------
# Auto-install missing dependencies
# ---------------------------------------------------------------------------

def _ensure_deps():
    """Install missing Python packages and update yt-dlp at startup."""
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
        print(f"[SETUP] Installing missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
        print("[SETUP] Package installation complete.")

    try:
        print("[SETUP] Upgrading yt-dlp to latest release...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"])
    except Exception as e:
        print(f"[SETUP] yt-dlp check warning: {e}")

_ensure_deps()

# ---------------------------------------------------------------------------
# Core Imports
# ---------------------------------------------------------------------------

import asyncio
import re
import time
import json
import math
import random
import base64
from pathlib import Path
from urllib.request import Request, urlopen
from collections import deque
from typing import Union, Dict, Any, Tuple, List

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import FloodWait
from pyrogram.raw.types import InputFileBig, InputFile
from pyrogram.raw.functions.upload import SaveBigFilePart
from dotenv import load_dotenv
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ---------------------------------------------------------------------------
# Global State Tracking
# ---------------------------------------------------------------------------

cancelled_tasks = set()
active_processes = {}
user_states = {}
progress_data = {}

def _cleanup_stale_user_states():
    """Prune user states older than 15 minutes to prevent RAM leak from abandoned setups."""
    now = time.time()
    stale_users = [
        u for u, s in user_states.items()
        if now - s.get("created_at", now) > 900
    ]
    for u in stale_users:
        user_states.pop(u, None)

# ---------------------------------------------------------------------------
# Configuration & Setup
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OWNER_GROUP = int(os.getenv("OWNER_GROUP", "0"))

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
RESTART_FILE = BASE_DIR / "restart.json"

if DOWNLOAD_DIR.exists():
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/116.0.0.0 Safari/537.36",
    "Referer": "https://appx-play.akamai.net.in/",
    "Origin": "https://appx-play.akamai.net.in",
    "x-requested-with": "mark.via.gp"
}

app = Client(
    "pdf_video_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(BASE_DIR),
)

from urllib.parse import urlparse, unquote

def extract_name_from_url(url: str, default_index: int = 1) -> str:
    """Extract a clean title from a URL path or default to a clean lesson name."""
    try:
        url = decrypt_classx_url(url)
        path = urlparse(url).path
        filename = Path(path).name
        if filename:
            clean_name = re.sub(r'\.(pdf|m3u8|mp4|ts|mkv|webm)$', '', filename, flags=re.IGNORECASE)
            clean_name = unquote(clean_name).replace('_', ' ').replace('-', ' ').strip()
            # Ignore generic technical terms or random master/index names
            if clean_name and len(clean_name) > 3 and clean_name.lower() not in ["master", "index", "playlist", "video", "file"]:
                return clean_name
    except Exception:
        pass
    return f"Lesson {default_index}"

# ---------------------------------------------------------------------------
# Helper Utility Functions
# ---------------------------------------------------------------------------

def is_pdf_url(url: str) -> bool:
    """Check if URL points to a PDF document."""
    url_lower = url.lower()
    return ".pdf" in url_lower or "pdf" in url_lower

def clean_b64(s: str) -> str:
    """Fix base64 string padding."""
    if not s:
        return ""
    s = str(s).replace("\\/", "/").replace("\\", "").strip()
    return s + "=" * ((4 - len(s) % 4) % 4)

def decrypt_classx_url(encrypted_str: str) -> str:
    """Decrypt ClassX or encrypted stream/PDF links."""
    if not encrypted_str or not isinstance(encrypted_str, str):
        return ""
    encrypted_str = encrypted_str.strip()
    if encrypted_str.startswith("http"):
        return encrypted_str
        
    key = b"6385731993184651"
    if ":" in encrypted_str:
        parts = encrypted_str.split(":", 1)
        cipher_b64 = clean_b64(parts[0])
        iv_b64 = clean_b64(parts[1])
        try:
            iv = base64.b64decode(iv_b64)
            if len(iv) >= 16:
                iv = iv[:16]
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(base64.b64decode(cipher_b64))
            decrypted_str = unpad(decrypted, AES.block_size).decode("utf-8", errors="ignore").strip()
            if decrypted_str.startswith("http"):
                return decrypted_str
        except Exception:
            pass
    return encrypted_str

def format_bytes(size: int) -> str:
    """Format size in bytes to human readable string with 2 decimal places."""
    if size <= 0:
        return "0.00 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size, 1024)))
    p = math.pow(1024, i)
    s = round(size / p, 2)
    return f"{s:.2f} {units[i]}"

def get_readable_time(seconds: int) -> str:
    """Format seconds into HH:MM:SS string."""
    if seconds <= 0:
        return "0s"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

# ---------------------------------------------------------------------------
# Progress Loop Updater
# ---------------------------------------------------------------------------

async def _progress_loop(status_msg: Message, task_id: str, title: str = "", is_pdf: bool = False):
    """Background task updating message progress in real-time with custom layout."""
    start_time = time.time()
    last_update = 0
    
    while task_id in progress_data:
        await asyncio.sleep(2)
        if task_id in cancelled_tasks:
            break
            
        data = progress_data.get(task_id, {})
        phase = data.get("phase", "Downloading")
        current = data.get("current", 0)
        total = data.get("total", 0)
        
        now = time.time()
        elapsed = now - start_time
        
        media_type = "PDF" if is_pdf else "Video"
        if " " in phase:
            header = f"{phase}..." if not phase.endswith("...") else phase
        else:
            header = f"{phase} {media_type}..."
        
        if total > 0:
            speed = current / elapsed if elapsed > 0 else 0
            text = (
                f"{header}\n"
                f"{title}\n\n"
                f"{format_bytes(current)} / {format_bytes(total)}\n"
                f"Speed: {format_bytes(int(speed))}/s"
            )
        else:
            speed = current / elapsed if elapsed > 0 else 0
            text = (
                f"{header}\n"
                f"{title}\n\n"
                f"{format_bytes(current)}\n"
                f"Speed: {format_bytes(int(speed))}/s"
            )
            
        if now - last_update >= 3:
            try:
                await status_msg.edit_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Stop", callback_data=f"cancel_{task_id}")]
                    ])
                )
                last_update = now
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Downloaders (PDF & Video)
# ---------------------------------------------------------------------------

async def download_pdf(url: str, output_path: str, task_id: str) -> Tuple[bool, str]:
    """Download direct PDF file."""
    try:
        url = decrypt_classx_url(url)
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=60) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            progress_data[task_id] = {"phase": "Downloading", "current": 0, "total": total_size}
            
            downloaded = 0
            with open(output_path, "wb") as f:
                while True:
                    if task_id in cancelled_tasks:
                        return False, "Cancelled by user"
                    chunk = resp.read(1024 * 128)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if task_id in progress_data:
                        progress_data[task_id]["current"] = downloaded
            return True, ""
    except Exception as e:
        return False, str(e)

async def download_video(url: str, output_path: str, task_id: str, quality: str = "720") -> Tuple[bool, str]:
    """Download video stream using yt-dlp with real-time disk monitoring."""
    disk_monitor = None
    try:
        url = decrypt_classx_url(url)
        format_spec = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best/b"
        
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--newline",
            "--no-check-certificates",
            "--no-warnings",
            "--user-agent", HEADERS["User-Agent"],
            "--referer", HEADERS["Referer"],
            "--concurrent-fragments", "8",
            "-f", format_spec,
            "--merge-output-format", "mp4",
            "-o", output_path,
            url
        ]
        
        for k, v in HEADERS.items():
            if k not in ["User-Agent", "Referer"]:
                cmd.extend(["--add-header", f"{k}:{v}"])
            
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=10 * 1024 * 1024
        )
        active_processes[task_id] = process
        progress_data[task_id] = {"phase": "Downloading", "current": 0, "total": 0}
        
        output_logs = deque(maxlen=50)  # Fixed size buffer to prevent memory leak from unconstrained log growth

        # Real-time disk size monitor fallback
        async def monitor_disk_size():
            out_file = Path(output_path)
            parent_dir = out_file.parent
            file_prefix = out_file.name
            
            while task_id in progress_data and process.returncode is None:
                await asyncio.sleep(1)
                try:
                    current_bytes = 0
                    if out_file.exists():
                        current_bytes = out_file.stat().st_size
                    else:
                        for p in parent_dir.glob(f"{file_prefix}*"):
                            current_bytes = max(current_bytes, p.stat().st_size)
                            
                    if current_bytes > 0 and task_id in progress_data:
                        if progress_data[task_id]["current"] < current_bytes:
                            progress_data[task_id]["current"] = current_bytes
                except Exception:
                    pass

        disk_monitor = asyncio.create_task(monitor_disk_size())

        while True:
            if task_id in cancelled_tasks:
                try:
                    process.kill()
                except Exception:
                    pass
                return False, "Cancelled by user"
                
            try:
                line = await process.stdout.readline()
            except ValueError:
                line = await process.stdout.read(65536)
                
            if not line:
                break
            line_str = line.decode("utf-8", errors="ignore").strip()
            if line_str:
                output_logs.append(line_str)
            
            # Parse yt-dlp percentage lines with --newline
            match = re.search(r'\[download\]\s+(\d+\.?\d*)%\s+of\s+~?\s*(\d+\.?\d*)(\w+)', line_str)
            if match:
                pct = float(match.group(1))
                size_num = float(match.group(2))
                unit = match.group(3).upper()
                mult = 1024 * 1024 if "M" in unit else (1024 * 1024 * 1024 if "G" in unit else (1024 if "K" in unit else 1))
                total_b = int(size_num * mult)
                curr_b = int((pct / 100.0) * total_b)
                if task_id in progress_data:
                    progress_data[task_id]["total"] = total_b
                    progress_data[task_id]["current"] = max(progress_data[task_id]["current"], curr_b)

        await process.wait()
        
        if process.returncode != 0:
            err_log = "\n".join(output_logs)
            return False, err_log or "yt-dlp video download failed"
            
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        active_processes.pop(task_id, None)
        if disk_monitor and not disk_monitor.done():
            disk_monitor.cancel()

# ---------------------------------------------------------------------------
# Video Metadata & Thumbnail Generators
# ---------------------------------------------------------------------------

def get_video_metadata(filepath: str) -> Tuple[int, int, int]:
    """Extract duration, width, height using ffprobe."""
    duration, width, height = 0, 0, 0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        res = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=15).decode("utf-8").splitlines()
        if len(res) >= 1:
            try:
                width = int(res[0])
            except ValueError:
                pass
        if len(res) >= 2:
            try:
                height = int(res[1])
            except ValueError:
                pass
        if len(res) >= 3:
            try:
                duration = int(float(res[2]))
            except ValueError:
                pass
    except Exception:
        pass
    return duration, width, height

def generate_thumbnail(video_path: str, thumb_path: str) -> str:
    """Generate thumbnail image from video."""
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:02",
            "-i", video_path, "-vframes", "1",
            "-vf", "scale=320:-1", thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    return ""

# ---------------------------------------------------------------------------
# Command Handlers (/start, /update, /upate, /stop, /cancel)
# ---------------------------------------------------------------------------

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    """Start command handler."""
    _cleanup_stale_user_states()
    user = message.from_user
    mention = user.mention if user else "User"
    welcome_text = (
        f"Hello {mention}!\n\n"
        f"Welcome to PDF & Video Downloader Bot\n\n"
        f"Supported Links:\n"
        f"• PDF Documents (Direct PDF, Google Drive PDF, ClassX encrypted PDF)\n"
        f"• Video Streams (YouTube, m3u8, MP4, ClassX/Appx DRM, Drive Video)\n\n"
        f"Usage Guide:\n"
        f"1. Upload a .txt file containing Name : URL or URL links.\n"
        f"2. Or send a direct PDF or Video URL in chat."
    )
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Help & Guide", callback_data="help_menu"),
            InlineKeyboardButton("Commands", callback_data="commands_menu")
        ],
        [
            InlineKeyboardButton("Update Bot", callback_data="update_bot"),
            InlineKeyboardButton("Owner", user_id=OWNER_ID if OWNER_ID else 1934839437)
        ]
    ])
    await message.reply_text(welcome_text, reply_markup=buttons)

@app.on_message(filters.command(["update", "upate"]))
async def update_cmd(client: Client, message: Message):
    """Update command handler to pull code, upgrade yt-dlp, and restart."""
    _cleanup_stale_user_states()
    if OWNER_ID and message.from_user.id != OWNER_ID:
        await message.reply_text("Only the bot owner can execute /update command.")
        return
        
    msg = await message.reply_text("Starting Bot Update...\n\n1. Pulling latest code via Git...")
    
    git_out = ""
    try:
        res = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
        git_out = res.stdout or res.stderr
    except Exception as e:
        git_out = f"Git error: {e}"
        
    await msg.edit_text(f"Upgrading yt-dlp...\n\nGit output:\n{git_out[:300]}")
    
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], capture_output=True, timeout=60)
    except Exception:
        pass
        
    await msg.edit_text("Update complete! Restarting process...")
    
    # Save notification state
    RESTART_FILE.write_text(json.dumps({"chat_id": message.chat.id, "time": time.time()}))
    
    # Restart python script
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command(["stop", "cancel"]))
async def cancel_cmd(client: Client, message: Message):
    """Cancel ongoing download tasks."""
    _cleanup_stale_user_states()
    user_id = message.from_user.id
    if user_id in user_states:
        user_states.pop(user_id, None)
        await message.reply_text("Active setup state cleared.")
    
    cancelled = False
    for task_id in list(progress_data.keys()):
        if task_id.startswith(str(message.chat.id)):
            cancelled_tasks.add(task_id)
            if task_id in active_processes:
                try:
                    active_processes[task_id].kill()
                except Exception:
                    pass
            cancelled = True
            
    if cancelled:
        await message.reply_text("Stopped active download task!")
    else:
        await message.reply_text("No active task running.")

# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------

@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    _cleanup_stale_user_states()
    data = query.data
    user_id = query.from_user.id
    
    if data == "help_menu":
        text = (
            "Help & Instructions\n\n"
            "• Batch Downloading: Send a .txt file formatted with links:\n"
            "  Topic Name : https://example.com/video.m3u8\n"
            "  Document Title : https://example.com/file.pdf\n\n"
            "• Single Download: Send any direct PDF or Video link in chat.\n"
            "• Supported Types: PDF documents & Video streams.\n"
            "• Edit Caption: Reply to any uploaded Video or PDF with any text to edit its caption.\n"
            "• Cancel: Click Stop on progress card or send /stop."
        )
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="start_menu")]])
        await query.message.edit_text(text, reply_markup=buttons)
        
    elif data == "commands_menu":
        text = (
            "Available Commands:\n\n"
            "• /start - Launch interactive bot menu\n"
            "• /update (or /upate) - Pull updates & restart bot\n"
            "• /stop (or /cancel) - Cancel current active task"
        )
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="start_menu")]])
        await query.message.edit_text(text, reply_markup=buttons)
        
    elif data == "start_menu":
        mention = query.from_user.mention
        welcome_text = (
            f"Hello {mention}!\n\n"
            f"Welcome to PDF & Video Downloader Bot\n\n"
            f"Supported Links:\n"
            f"• PDF Documents\n"
            f"• Video Streams\n\n"
            f"Send a .txt file or link to get started!"
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Help & Guide", callback_data="help_menu"),
                InlineKeyboardButton("Commands", callback_data="commands_menu")
            ],
            [
                InlineKeyboardButton("Update Bot", callback_data="update_bot"),
                InlineKeyboardButton("Owner", user_id=OWNER_ID if OWNER_ID else 1934839437)
            ]
        ])
        await query.message.edit_text(welcome_text, reply_markup=buttons)
        
    elif data == "update_bot":
        if OWNER_ID and user_id != OWNER_ID:
            await query.answer("Owner only command", show_alert=True)
            return
        await query.answer("Starting update process...")
        await update_cmd(client, query.message)
        
    elif data.startswith("cancel_"):
        task_id = data.replace("cancel_", "")
        cancelled_tasks.add(task_id)
        if task_id in active_processes:
            try:
                active_processes[task_id].kill()
            except Exception:
                pass
        await query.answer("Task cancellation requested!", show_alert=True)
        
    elif data.startswith("res_"):
        res = data.replace("res_", "")
        state = user_states.get(user_id)
        if state and state.get("step") == "awaiting_quality":
            state["quality"] = res
            state["step"] = "processing"
            await query.message.delete()
            asyncio.create_task(run_batch_download(client, query.message, state))

# ---------------------------------------------------------------------------
# Message Handlers (Documents / TXT & Links)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Message Handlers (Direct Link & Caption Edit)
# ---------------------------------------------------------------------------

@app.on_message(filters.document)
async def document_handler(client: Client, message: Message):
    await message.reply_text("Please send a direct PDF or Video URL in chat.")

@app.on_message(filters.text & ~filters.command(["start", "update", "upate", "stop", "cancel"]))
async def text_handler(client: Client, message: Message):
    text = message.text.strip()
    
    # 0. Reply Handler: Edit Caption of Video or PDF message sent by bot
    if message.reply_to_message:
        replied_msg = message.reply_to_message
        if replied_msg.from_user and replied_msg.from_user.is_self:
            if replied_msg.video or replied_msg.document:
                try:
                    await client.edit_message_caption(
                        chat_id=message.chat.id,
                        message_id=replied_msg.id,
                        caption=text
                    )
                    await message.reply_text("Caption updated successfully.")
                    return
                except Exception as e:
                    await message.reply_text(f"Failed to update caption: {e}")
                    return

    # 1. Single Direct Link Input
    if ":" in text and not text.startswith("http"):
        parts = text.split(":", 1)
        name = parts[0].strip()
        url = parts[1].strip()
    else:
        url = text
        name = extract_name_from_url(url, 1)
        
    if url.startswith("http://") or url.startswith("https://") or ":" in url:
        item = {"index": 1, "name": name, "url": url}
        asyncio.create_task(process_single_item(client, message.chat.id, item, quality="720"))
    else:
        await message.reply_text("Please send a valid PDF or Video URL.")

# ---------------------------------------------------------------------------
# Processing Engine (PDF & Video Download & Upload)
# ---------------------------------------------------------------------------

async def process_single_item(client: Client, chat_id: int, item: Dict[str, Any], quality: str = "720"):
    """Process a single PDF or Video link item."""
    name = item.get("name", "File")
    raw_url = item.get("url", "")
    url = decrypt_classx_url(raw_url)
    task_id = f"{chat_id}_{item['index']}_{int(time.time())}"
    
    is_pdf = is_pdf_url(url)
    ext = ".pdf" if is_pdf else ".mp4"
    safe_name = re.sub(r'[\\/*?:"<>|]', "", name).strip() or "file"
    filename = f"{safe_name}{ext}"
    output_path = str(DOWNLOAD_DIR / f"{task_id}{ext}")
    
    status_msg = await client.send_message(chat_id, f"Starting download for: {name}")
    progress_data[task_id] = {"phase": "Downloading", "current": 0, "total": 0}
    updater = asyncio.create_task(_progress_loop(status_msg, task_id, title=name, is_pdf=is_pdf))
    
    try:
        if is_pdf:
            success, err = await download_pdf(url, output_path, task_id)
        else:
            success, err = await download_video(url, output_path, task_id, quality=quality)
            
        if not success:
            await status_msg.edit_text(f"Download Failed: {name}\n\n{err[:1000]}")
            return
            
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            await status_msg.edit_text("Error: Downloaded file is empty.")
            return
            
        file_size = os.path.getsize(output_path)
        progress_data[task_id] = {"phase": "Uploading", "current": 0, "total": file_size}
        
        async def upload_prog(current, total, t_id):
            if t_id in progress_data:
                progress_data[t_id]["current"] = current
                progress_data[t_id]["total"] = total
                
        if is_pdf:
            await client.send_document(
                chat_id=chat_id,
                document=output_path,
                file_name=filename,
                caption=f"{name}",
                progress=upload_prog,
                progress_args=(task_id,)
            )
        else:
            duration, width, height = await asyncio.to_thread(get_video_metadata, output_path)
            thumb_file = str(DOWNLOAD_DIR / f"thumb_{task_id}.jpg")
            thumb = await asyncio.to_thread(generate_thumbnail, output_path, thumb_file)
            
            try:
                await client.send_video(
                    chat_id=chat_id,
                    video=output_path,
                    file_name=filename,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb if thumb and os.path.exists(thumb) else None,
                    caption=f"{name} [{quality}p]",
                    supports_streaming=True,
                    progress=upload_prog,
                    progress_args=(task_id,)
                )
            finally:
                if thumb and os.path.exists(thumb):
                    try:
                        os.remove(thumb)
                    except OSError:
                        pass

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        await status_msg.edit_text(f"Error processing {name}:\n{e}")
    finally:
        progress_data.pop(task_id, None)
        cancelled_tasks.discard(task_id)
        if updater and not updater.done():
            updater.cancel()
            
        # Clean up all matching download files (including .part, .ytdl, .jpg)
        try:
            for p in DOWNLOAD_DIR.glob(f"{task_id}*"):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Restart Notification & Entry Point
# ---------------------------------------------------------------------------

@app.on_message(group=-1)
async def check_restart_notification(client: Client, message: Message):
    if RESTART_FILE.exists():
        try:
            RESTART_FILE.unlink()
        except Exception as e:
            print(f"[RESTART] Notification error: {e}")
    message.continue_propagation()

if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("ERROR: BOT_TOKEN, API_ID, or API_HASH missing from .env")
        sys.exit(1)

    print("Bot started successfully. Listening for commands...")
    app.run()

# ---------------------------------------------------------------------------
# Restart Notification & Entry Point
# ---------------------------------------------------------------------------

@app.on_message(group=-1)
async def check_restart_notification(client: Client, message: Message):
    if RESTART_FILE.exists():
        try:
            RESTART_FILE.unlink()
        except Exception as e:
            print(f"[RESTART] Notification error: {e}")
    message.continue_propagation()

if __name__ == "__main__":
    if not BOT_TOKEN or not API_ID or not API_HASH:
        print("ERROR: BOT_TOKEN, API_ID, or API_HASH missing from .env")
        sys.exit(1)

    print("Bot started successfully. Listening for commands...")
    app.run()
