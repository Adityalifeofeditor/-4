import os
import asyncio
import time
import json
import logging
import traceback
import platform
import psutil
import requests
import shutil
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from enum import Enum

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.handlers import CallbackQueryHandler

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ServerSelectionTimeoutError

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("AudioQueueBot")

# ================= CONFIG =================
API_ID = int(os.getenv("API_ID", 27169529))
API_HASH = os.getenv("API_HASH", "5d67602a4e0bbfabe669c0febeaf63b6")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8574806355:AAGOXL5nDpzMvaEdhBAR_4vw3N2NXDABuJs")
OWNER_ID = int(os.getenv("OWNER_ID", 6441347235))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", str(OWNER_ID)).split(" ") if x]
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://adam822728:iP9ESt5vyfwDRxNB@cluster0.r82vfuz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

DOWNLOAD_DIR = "downloads"
MAX_CONCURRENT_WORKERS = 1
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BOT_START_TIME = time.time()
USER_WORKERS: Dict[int, asyncio.Task] = {}
USER_STATES: Dict[int, Dict] = {}

# ================= ENUMS =================
class UploadType(Enum):
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"

# ================= BOT ====================
app = Client(
    "audio_queue_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50,
    max_concurrent_transmissions=5
)

# ================= DATABASE =================
try:
    mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo.server_info()  # Test connection
    db = mongo.audio_bot
    queue_col = db.queue
    stats_col = db.stats
    users_col = db.users
    settings_col = db.settings
    admin_col = db.admin
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    exit(1)

# ================= INITIALIZE DATABASE =================
async def initialize_db():
    """Initialize database collections and indexes"""
    try:
        # Create indexes
        await users_col.create_index("user_id", unique=True)
        await settings_col.create_index("user_id", unique=True)
        await admin_col.create_index("key", unique=True)
        
        # Initialize admin settings
        await admin_col.update_one(
            {"key": "free_mode"},
            {"$set": {"value": True, "updated_at": datetime.now()}},
            upsert=True
        )
        
        # Initialize owner as admin
        for admin_id in ADMIN_IDS:
            await users_col.update_one(
                {"user_id": admin_id},
                {"$set": {"is_admin": True, "is_premium": True, "is_banned": False}},
                upsert=True
            )
        
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")

# ================= USER MANAGEMENT =================
async def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    if user_id in ADMIN_IDS:
        return True
    user = await users_col.find_one({"user_id": user_id})
    return user.get("is_admin", False) if user else False

async def is_premium(user_id: int) -> bool:
    """Check if user is premium"""
    # Check free mode
    free_mode = await admin_col.find_one({"key": "free_mode"})
    if free_mode and free_mode.get("value", True):
        return True
    
    user = await users_col.find_one({"user_id": user_id})
    return user.get("is_premium", False) if user else False

async def is_banned(user_id: int) -> bool:
    """Check if user is banned"""
    user = await users_col.find_one({"user_id": user_id})
    return user.get("is_banned", False) if user else False

async def ban_user(user_id: int, admin_id: int):
    """Ban a user"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": True, "banned_by": admin_id, "banned_at": datetime.now()}},
        upsert=True
    )

async def unban_user(user_id: int):
    """Unban a user"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": False}, "$unset": {"banned_by": "", "banned_at": ""}}
    )

async def add_premium(user_id: int, admin_id: int):
    """Add premium to user"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_premium": True, "premium_by": admin_id, "premium_at": datetime.now()}},
        upsert=True
    )

async def remove_premium(user_id: int):
    """Remove premium from user"""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_premium": False}, "$unset": {"premium_by": "", "premium_at": ""}}
    )

async def get_premium_users() -> List[Dict]:
    """Get list of premium users"""
    cursor = users_col.find({"is_premium": True})
    return await cursor.to_list(length=None)

async def get_log_channel() -> Optional[int]:
    """Get log channel ID"""
    setting = await admin_col.find_one({"key": "log_channel"})
    return setting.get("value") if setting else None

async def set_log_channel(channel_id: int):
    """Set log channel"""
    await admin_col.update_one(
        {"key": "log_channel"},
        {"$set": {"value": channel_id, "updated_at": datetime.now()}},
        upsert=True
    )

async def get_free_mode() -> bool:
    """Get free mode status"""
    setting = await admin_col.find_one({"key": "free_mode"})
    return setting.get("value", True) if setting else True

async def toggle_free_mode():
    """Toggle free mode"""
    current = await get_free_mode()
    await admin_col.update_one(
        {"key": "free_mode"},
        {"$set": {"value": not current, "updated_at": datetime.now()}},
        upsert=True
    )
    return not current

# ================= USER SETTINGS =================
async def get_user_settings(user_id: int) -> Dict:
    """Get user settings"""
    settings = await settings_col.find_one({"user_id": user_id})
    if not settings:
        # Default settings
        default_settings = {
            "user_id": user_id,
            "compression": True,
            "upload_type": "audio",
            "thumb_url": "",
            "rename_format": "{original}",
            "remove_text": "",
            "replace_text": "",
            "replace_with": "",
            "suffix": "",
            "prefix": "",
            "caption_header": "",
            "caption_footer": "",
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        await settings_col.insert_one(default_settings)
        return default_settings
    return settings

async def update_user_settings(user_id: int, updates: Dict):
    """Update user settings"""
    updates["updated_at"] = datetime.now()
    await settings_col.update_one(
        {"user_id": user_id},
        {"$set": updates},
        upsert=True
    )

async def apply_filename_format(original_name: str, settings: Dict) -> str:
    """Apply filename formatting based on settings"""
    name, ext = os.path.splitext(original_name)
    
    # Apply remove text
    remove_text = settings.get("remove_text", "")
    if remove_text:
        name = name.replace(remove_text, "")
    
    # Apply replace text
    replace_text = settings.get("replace_text", "")
    replace_with = settings.get("replace_with", "")
    if replace_text and replace_with:
        name = name.replace(replace_text, replace_with)
    
    # Apply prefix and suffix
    prefix = settings.get("prefix", "")
    suffix = settings.get("suffix", "")
    name = f"{prefix}{name}{suffix}"
    
    # Apply rename format
    rename_format = settings.get("rename_format", "{original}")
    if rename_format != "{original}":
        name = rename_format
    
    return f"{name}{ext}"

async def apply_caption_format(original_caption: str, settings: Dict) -> str:
    """Apply caption formatting based on settings"""
    caption = original_caption or ""
    
    # Apply remove text
    remove_text = settings.get("remove_text", "")
    if remove_text:
        caption = caption.replace(remove_text, "")
    
    # Apply replace text
    replace_text = settings.get("replace_text", "")
    replace_with = settings.get("replace_with", "")
    if replace_text and replace_with:
        caption = caption.replace(replace_text, replace_with)
    
    # Apply header and footer
    header = settings.get("caption_header", "")
    footer = settings.get("caption_footer", "")
    
    if header:
        caption = f"{header}\n{caption}" if caption else header
    if footer:
        caption = f"{caption}\n{footer}" if caption else footer
    
    return caption

# ================= UTILS ==================
def format_uptime(sec: int) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    out = []
    if d: out.append(f"{d}d")
    if h: out.append(f"{h}h")
    if m: out.append(f"{m}m")
    out.append(f"{s}s")
    return " ".join(out)

def progress_bar(current, total):
    if total <= 0:
        return "[░░░░░░░░░░] 0%"
    percent = min((current / total) * 100, 100)
    filled = int(percent // 10)
    return f"[{'█'*filled}{'░'*(10-filled)}] {percent:.1f}%"

async def progress(current, total, msg, start, stage, qinfo):
    try:
        elapsed = time.time() - start
        speed = current / elapsed if elapsed > 0 else 0
        eta = int((total - current) / speed) if total > 0 and speed > 0 else 0
        
        text = (
            f"🎧 **{stage}**\n"
            f"{progress_bar(current, total)}\n"
            f"📊 `{human_readable_size(current)} / {human_readable_size(total)}`\n"
            f"⚡ `{human_readable_size(speed)}/s`\n"
            f"⏳ ETA: `{eta}s`\n"
            f"🧵 {qinfo}"
        )
        
        try:
            await msg.edit(text)
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)
            
    except Exception as e:
        logger.debug(f"Progress error: {e}")

def human_readable_size(size):
    """Convert bytes to human readable format"""
    if not size:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

async def get_system_stats():
    """Get comprehensive system statistics"""
    try:
        # CPU usage
        cpu_percent = psutil.cpu_percent(interval=0.5)
        
        # Memory usage
        memory = psutil.virtual_memory()
        
        # Disk usage
        disk = psutil.disk_usage('/')
        
        # Bot process info
        process = psutil.Process(os.getpid())
        process_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        # Network
        net_io = psutil.net_io_counters()
        
        # Uptime
        uptime = time.time() - BOT_START_TIME
        
        # Queue stats
        total_users = await queue_col.count_documents({})
        total_queued = 0
        async for user in queue_col.find({}):
            total_queued += len(user.get('queue', []))
        
        # User stats
        total_banned = await users_col.count_documents({"is_banned": True})
        total_premium = await users_col.count_documents({"is_premium": True})
        free_mode = await get_free_mode()
        
        # MongoDB stats
        mongo_status = "✅ Online"
        try:
            await mongo.admin.command('ping')
        except:
            mongo_status = "❌ Offline"
        
        return {
            'cpu': cpu_percent,
            'memory': memory.percent,
            'memory_used': memory.used / 1024 / 1024 / 1024,  # GB
            'memory_total': memory.total / 1024 / 1024 / 1024,  # GB
            'disk': disk.percent,
            'disk_used': disk.used / 1024 / 1024 / 1024,  # GB
            'disk_total': disk.total / 1024 / 1024 / 1024,  # GB
            'process_memory': process_memory,
            'bytes_sent': net_io.bytes_sent,
            'bytes_recv': net_io.bytes_recv,
            'uptime': uptime,
            'total_users': total_users,
            'total_queued': total_queued,
            'active_workers': len(USER_WORKERS),
            'total_banned': total_banned,
            'total_premium': total_premium,
            'free_mode': free_mode,
            'mongo_status': mongo_status,
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting system stats: {e}")
        return None

# ================= AUDIO PROCESSING ==================
async def compress_audio(input_file: str, output_file: str, bitrate="128k"):
    if not os.path.exists(input_file):
        raise FileNotFoundError("Input file missing")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_file,
        "-map_metadata", "-1",
        "-vn",
        "-ac", "2",
        "-b:a", bitrate,
        "-f", "mp3",
        output_file
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, err = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"FFmpeg error: {err.decode(errors='ignore')}")
        raise RuntimeError(f"FFmpeg compression failed: {proc.returncode}")

    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        raise RuntimeError("Compressed file invalid")

async def get_audio_duration(path: str) -> int:
    if not os.path.exists(path):
        return 0

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        path
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    out, err = await proc.communicate()

    try:
        data = json.loads(out)
        return int(float(data["format"]["duration"]))
    except Exception:
        return 0

# ================= QUEUE MANAGEMENT ==================
async def add_queue(user_id: int, task: dict):
    """Add task to user's queue"""
    await queue_col.update_one(
        {"_id": user_id},
        {
            "$push": {"queue": task},
            "$setOnInsert": {
                "active": False,
                "cancel": False,
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            }
        },
        upsert=True
    )
    
    # Update stats
    await stats_col.update_one(
        {"_id": "total_files"},
        {"$inc": {"count": 1}},
        upsert=True
    )

async def get_queue_status(user_id: int) -> dict:
    """Get user's queue status"""
    data = await queue_col.find_one({"_id": user_id})
    if not data:
        return {"total": 0, "position": 0, "queue": []}
    
    queue = data.get("queue", [])
    return {
        "total": len(queue),
        "position": 1 if queue else 0,
        "queue": queue,
        "active": data.get("active", False),
        "cancel": data.get("cancel", False)
    }

async def cancel_user_queue(user_id: int, cancel_all: bool = False):
    """Cancel user's queue processing"""
    if cancel_all:
        # Remove all tasks
        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"queue": [], "active": False, "cancel": False}}
        )
    else:
        # Mark for cancellation
        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"cancel": True}}
        )
    
    # Cancel worker task if exists
    if user_id in USER_WORKERS:
        try:
            USER_WORKERS[user_id].cancel()
        except:
            pass
        del USER_WORKERS[user_id]

async def worker(user_id: int, chat_id: int):
    """Worker to process user's queue sequentially"""
    worker_task = asyncio.current_task()
    USER_WORKERS[user_id] = worker_task
    
    try:
        # Check if user is banned
        if await is_banned(user_id):
            await app.send_message(chat_id, "❌ **You are banned from using this bot!**")
            return
        
        # Check premium status if free mode is off
        free_mode = await get_free_mode()
        if not free_mode and not await is_premium(user_id):
            await app.send_message(
                chat_id,
                "⚠️ **Premium Required**\n\n"
                "Free mode is currently disabled. You need premium to use this bot.\n"
                "Contact the bot owner for premium access."
            )
            return
        
        # Check if already active
        data = await queue_col.find_one({"_id": user_id})
        if data and data.get("active"):
            logger.info(f"Worker already active for user {user_id}")
            return
        
        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"active": True, "cancel": False}}
        )
        
        # Get user settings
        user_settings = await get_user_settings(user_id)
        
        while True:
            # Get current queue state
            data = await queue_col.find_one({"_id": user_id})
            if not data:
                break
                
            queue = data.get("queue", [])
            total = len(queue)
            
            if not queue:
                break
                
            if data.get("cancel"):
                await app.send_message(chat_id, "❌ Queue processing cancelled")
                break
            
            # Process first task
            task = queue[0]
            original_filename = task.get("file_name", "Unknown")
            file_id = task.get("file_id")
            
            # Apply filename formatting
            formatted_filename = await apply_filename_format(original_filename, user_settings)
            
            # Create status message with buttons
            status_msg = await app.send_message(
                chat_id,
                f"📥 **Downloading:** `{formatted_filename}`\n"
                f"📊 **Queue:** 1/{total}\n"
                f"⚙️ **Settings:** {'Compress' if user_settings['compression'] else 'No Compress'} | {user_settings['upload_type'].title()}\n"
                f"⏳ **Status:** Starting...",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                        InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
                    ]
                ])
            )
            
            start_time = time.time()
            
            try:
                # Download
                download_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(start_time)}_{original_filename}")
                
                await status_msg.edit(
                    f"📥 **Downloading:** `{formatted_filename}`\n"
                    f"📊 **Queue:** 1/{total}\n"
                    f"⏳ **Status:** Downloading...",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                            InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
                        ]
                    ])
                )
                
                download_start = time.time()
                input_path = await app.download_media(
                    file_id,
                    file_name=download_path,
                    progress=progress,
                    progress_args=(status_msg, download_start, "Downloading", f"Queue: 1/{total}")
                )
                
                if not input_path or not os.path.exists(input_path):
                    raise FileNotFoundError("Download failed")
                
                # Process based on settings
                upload_path = input_path
                if user_settings.get("compression", True):
                    # Compress audio
                    output_path = input_path.rsplit('.', 1)[0] + "_compressed.mp3"
                    
                    await status_msg.edit(
                        f"🎛 **Compressing:** `{formatted_filename}`\n"
                        f"📊 **Queue:** 1/{total}\n"
                        f"⏳ **Status:** Compressing...",
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                                InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
                            ]
                        ])
                    )
                    
                    await compress_audio(input_path, output_path)
                    upload_path = output_path
                
                # Get audio info
                duration = await get_audio_duration(upload_path)
                file_size = os.path.getsize(upload_path)
                
                # Apply caption formatting
                caption = await apply_caption_format("", user_settings)
                
                # Upload based on settings
                upload_type = user_settings.get("upload_type", "audio")
                
                await status_msg.edit(
                    f"📤 **Uploading:** `{formatted_filename}`\n"
                    f"📊 **Queue:** 1/{total}\n"
                    f"📏 **Size:** {human_readable_size(file_size)}\n"
                    f"⏱ **Duration:** {duration}s\n"
                    f"📦 **Type:** {upload_type.title()}\n"
                    f"⏳ **Status:** Uploading...",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                            InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
                        ]
                    ])
                )
                
                upload_start = time.time()
                
                # Prepare upload parameters
                upload_params = {
                    "chat_id": chat_id,
                    "progress": progress,
                    "progress_args": (status_msg, upload_start, "Uploading", f"Queue: 1/{total}")
                }
                
                # Add caption if available
                if caption:
                    upload_params["caption"] = caption
                
                # Add thumbnail if set
                thumb_url = user_settings.get("thumb_url", "")
                if thumb_url:
                    upload_params["thumb"] = thumb_url
                
                # Send based on upload type
                if upload_type == "audio":
                    await app.send_audio(
                        audio=upload_path,
                        duration=duration,
                        title=os.path.splitext(formatted_filename)[0],
                        performer="Audio Queue Bot",
                        **upload_params
                    )
                elif upload_type == "voice":
                    await app.send_voice(
                        voice=upload_path,
                        duration=duration,
                        **upload_params
                    )
                elif upload_type == "document":
                    await app.send_document(
                        document=upload_path,
                        file_name=formatted_filename,
                        **upload_params
                    )
                
                # Cleanup
                try:
                    os.remove(input_path)
                    if upload_path != input_path:
                        os.remove(upload_path)
                except:
                    pass
                
                # Update status
                elapsed = time.time() - start_time
                await status_msg.edit(
                    f"✅ **Completed:** `{formatted_filename}`\n"
                    f"📊 **Queue:** {total-1 if total > 1 else 0}/{total}\n"
                    f"⏱ **Time:** {elapsed:.1f}s\n"
                    f"✅ **Status:** Successfully processed"
                )
                
            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue
                
            except Exception as e:
                logger.error(f"Error processing file: {e}")
                logger.error(traceback.format_exc())
                
                await status_msg.edit(
                    f"❌ **Failed:** `{formatted_filename}`\n"
                    f"📊 **Queue:** 1/{total}\n"
                    f"💥 **Error:** {str(e)[:100]}...\n"
                    f"❌ **Status:** Processing failed"
                )
                
                # Cleanup on error
                try:
                    if 'input_path' in locals() and os.path.exists(input_path):
                        os.remove(input_path)
                    if 'upload_path' in locals() and upload_path != input_path and os.path.exists(upload_path):
                        os.remove(upload_path)
                except:
                    pass
                
                await asyncio.sleep(2)
                
            finally:
                # Remove processed task
                await queue_col.update_one(
                    {"_id": user_id},
                    {"$pop": {"queue": -1}}
                )
            
            # Small delay before next task
            await asyncio.sleep(1)
        
    except asyncio.CancelledError:
        logger.info(f"Worker cancelled for user {user_id}")
        raise
        
    except Exception as e:
        logger.critical(f"Worker error for user {user_id}: {e}")
        logger.critical(traceback.format_exc())
        
    finally:
        # Cleanup
        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"active": False, "cancel": False}}
        )
        
        if user_id in USER_WORKERS:
            del USER_WORKERS[user_id]

# ================= SETTINGS COMMAND =================
@app.on_message(filters.command("settings"))
async def settings_command(_, m: Message):
    """User settings command"""
    user_id = m.from_user.id
    
    # Check if banned
    if await is_banned(user_id):
        await m.reply("❌ **You are banned from using this bot!**")
        return
    
    settings = await get_user_settings(user_id)
    
    # Create settings menu
    text = (
        "⚙️ **User Settings**\n\n"
        "Customize how your files are processed:\n\n"
        f"**🎵 Compression:** `{'✅ ON' if settings['compression'] else '❌ OFF'}`\n"
        f"**📦 Upload Type:** `{settings['upload_type'].title()}`\n"
        f"**🖼 Thumbnail:** `{'✅ Set' if settings['thumb_url'] else '❌ Not Set'}`\n"
        f"**📝 Rename Format:** `{settings['rename_format']}`\n"
        f"**🔠 Text Remove:** `{settings['remove_text'] or 'Not Set'}`\n"
        f"**🔄 Text Replace:** `{settings['replace_text']} → {settings['replace_with']}`\n"
        f"**🔤 Prefix:** `{settings['prefix'] or 'None'}`\n"
        f"**🔣 Suffix:** `{settings['suffix'] or 'None'}`\n"
        f"**📄 Caption Header:** `{settings['caption_header'][:20] + '...' if settings['caption_header'] else 'None'}`\n"
        f"**📑 Caption Footer:** `{settings['caption_footer'][:20] + '...' if settings['caption_footer'] else 'None'}`\n\n"
        "Click the buttons below to configure each setting."
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Compression", callback_data="setting_compression"),
            InlineKeyboardButton("📦 Upload Type", callback_data="setting_upload_type")
        ],
        [
            InlineKeyboardButton("🖼 Thumbnail URL", callback_data="setting_thumb"),
            InlineKeyboardButton("📝 Rename File", callback_data="setting_rename")
        ],
        [
            InlineKeyboardButton("🗑 Remove Text", callback_data="setting_remove_text"),
            InlineKeyboardButton("🔄 Replace Text", callback_data="setting_replace_text")
        ],
        [
            InlineKeyboardButton("🔤 Prefix/Suffix", callback_data="setting_prefix_suffix"),
            InlineKeyboardButton("📄 Header/Footer", callback_data="setting_header_footer")
        ],
        [
            InlineKeyboardButton("📋 View Settings", callback_data="setting_view"),
            InlineKeyboardButton("🔄 Reset All", callback_data="setting_reset")
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="setting_close")
        ]
    ])
    
    await m.reply(text, reply_markup=keyboard)

# ================= ADMIN COMMAND =================
@app.on_message(filters.command("admin"))
async def admin_command(_, m: Message):
    """Admin panel command"""
    user_id = m.from_user.id
    
    if not await is_admin(user_id):
        await m.reply("❌ **Access Denied!**\nThis command is for administrators only.")
        return
    
    # Get stats for admin panel
    stats = await get_system_stats()
    free_mode = await get_free_mode()
    log_channel = await get_log_channel()
    
    text = (
        "👑 **Admin Control Panel**\n\n"
        f"**📊 System Status:**\n"
        f"• 🤖 Uptime: `{format_uptime(int(stats['uptime'])) if stats else 'N/A'}`\n"
        f"• 🖥 CPU: `{stats['cpu']:.1f}%` | 🧮 RAM: `{stats['memory']:.1f}%`\n"
        f"• 👥 Users: `{stats['total_users'] if stats else 0}` | 📁 Queue: `{stats['total_queued'] if stats else 0}`\n"
        f"• 👑 Premium: `{stats['total_premium'] if stats else 0}` | 🚫 Banned: `{stats['total_banned'] if stats else 0}`\n\n"
        f"**⚙️ Bot Settings:**\n"
        f"• 🌐 Free Mode: `{'✅ ON' if free_mode else '❌ OFF'}`\n"
        f"• 📢 Log Channel: `{log_channel or 'Not Set'}`\n\n"
        "Select an option below to manage the bot:"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
            InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")
        ],
        [
            InlineKeyboardButton("👑 Add Premium", callback_data="admin_add_premium"),
            InlineKeyboardButton("👤 Remove Premium", callback_data="admin_remove_premium")
        ],
        [
            InlineKeyboardButton("📋 View Premium", callback_data="admin_view_premium"),
            InlineKeyboardButton("🔁 Restart Bot", callback_data="admin_restart")
        ],
        [
            InlineKeyboardButton("📢 Log Channel", callback_data="admin_log_channel"),
            InlineKeyboardButton("🌐 Free Mode", callback_data="admin_free_mode")
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("❌ Close", callback_data="admin_close")
        ]
    ])
    
    await m.reply(text, reply_markup=keyboard)

# ================= SETTINGS CALLBACKS =================
@app.on_callback_query(filters.regex(r"^setting_"))
async def settings_callback_handler(_, query: CallbackQuery):
    """Handle settings callbacks"""
    user_id = query.from_user.id
    data = query.data
    
    try:
        if data == "setting_compression":
            settings = await get_user_settings(user_id)
            current = settings.get("compression", True)
            
            text = (
                f"🎵 **Compression Setting**\n\n"
                f"**Current:** `{'✅ ON' if current else '❌ OFF'}`\n\n"
                "**What it does:**\n"
                "• ✅ ON: Audio files will be compressed to reduce size\n"
                "• ❌ OFF: Files will be sent without compression\n\n"
                "**Note:** Compression reduces file size but may affect quality."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Enable", callback_data="toggle_compression_true"),
                    InlineKeyboardButton("❌ Disable", callback_data="toggle_compression_false")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_upload_type":
            settings = await get_user_settings(user_id)
            current = settings.get("upload_type", "audio")
            
            text = (
                f"📦 **Upload Type Setting**\n\n"
                f"**Current:** `{current.title()}`\n\n"
                "**Options:**\n"
                "• 🎵 Audio: Send as audio file (recommended)\n"
                "• 🎤 Voice: Send as voice message\n"
                "• 📄 Document: Send as document file\n\n"
                "**Note:** Voice messages are limited to 2MB on Telegram."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎵 Audio", callback_data="set_upload_audio"),
                    InlineKeyboardButton("🎤 Voice", callback_data="set_upload_voice"),
                    InlineKeyboardButton("📄 Document", callback_data="set_upload_document")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_thumb":
            settings = await get_user_settings(user_id)
            current = settings.get("thumb_url", "")
            
            text = (
                "🖼 **Thumbnail URL Setting**\n\n"
                f"**Current:** `{current or 'Not Set'}`\n\n"
                "**What it does:**\n"
                "• Sets a custom thumbnail for your uploaded files\n"
                "• Must be a direct image URL (JPG/PNG)\n\n"
                "**Examples:**\n"
                "• `https://example.com/image.jpg`\n"
                "• `https://i.imgur.com/xyz123.jpg`\n\n"
                "Click **Add** to set a new thumbnail or **Remove** to clear."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ Add", callback_data="add_thumb"),
                    InlineKeyboardButton("👁 View", callback_data="view_thumb"),
                    InlineKeyboardButton("🗑 Remove", callback_data="remove_thumb")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_rename":
            settings = await get_user_settings(user_id)
            current = settings.get("rename_format", "{original}")
            
            text = (
                "📝 **Rename File Setting**\n\n"
                f"**Current Format:** `{current}`\n\n"
                "**What it does:**\n"
                "• Renames files before uploading\n"
                "• Use `{original}` for original filename\n"
                "• Add custom text before/after\n\n"
                "**Examples:**\n"
                "• `MyAudio_{original}`\n"
                "• `{original}_converted`\n"
                "• `Custom_Name.mp3`\n\n"
                "Click **Add** to set a new format or **Remove** to reset."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ Add", callback_data="add_rename"),
                    InlineKeyboardButton("👁 View", callback_data="view_rename"),
                    InlineKeyboardButton("🗑 Remove", callback_data="remove_rename")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_remove_text":
            settings = await get_user_settings(user_id)
            current = settings.get("remove_text", "")
            
            text = (
                "🗑 **Remove Text Setting**\n\n"
                f"**Current:** `{current or 'Not Set'}`\n\n"
                "**What it does:**\n"
                "• Removes specific text from filenames\n"
                "• Works on both filename and caption\n\n"
                "**Example:**\n"
                "• Text to remove: `[Official]`\n"
                "• Before: `[Official] Song Name.mp3`\n"
                "• After: `Song Name.mp3`\n\n"
                "Click **Add** to set text to remove or **Remove** to clear."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ Add", callback_data="add_remove_text"),
                    InlineKeyboardButton("👁 View", callback_data="view_remove_text"),
                    InlineKeyboardButton("🗑 Remove", callback_data="remove_remove_text")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_replace_text":
            settings = await get_user_settings(user_id)
            replace_text = settings.get("replace_text", "")
            replace_with = settings.get("replace_with", "")
            
            text = (
                "🔄 **Replace Text Setting**\n\n"
                f"**Current:** `{replace_text}` → `{replace_with}`\n\n"
                "**What it does:**\n"
                "• Replaces specific text in filenames\n"
                "• Works on both filename and caption\n\n"
                "**Example:**\n"
                "• Replace: `old` with `new`\n"
                "• Before: `old_song.mp3`\n"
                "• After: `new_song.mp3`\n\n"
                "Click **Add** to set replacement rules or **Remove** to clear."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ Add", callback_data="add_replace_text"),
                    InlineKeyboardButton("👁 View", callback_data="view_replace_text"),
                    InlineKeyboardButton("🗑 Remove", callback_data="remove_replace_text")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_prefix_suffix":
            settings = await get_user_settings(user_id)
            prefix = settings.get("prefix", "")
            suffix = settings.get("suffix", "")
            
            text = (
                "🔤 **Prefix & Suffix Setting**\n\n"
                f"**Prefix:** `{prefix or 'None'}`\n"
                f"**Suffix:** `{suffix or 'None'}`\n\n"
                "**What it does:**\n"
                "• Prefix: Adds text to beginning of filename\n"
                "• Suffix: Adds text to end of filename\n\n"
                "**Examples:**\n"
                "• Prefix: `PRE_` → `PRE_filename.mp3`\n"
                "• Suffix: `_SUF` → `filename_SUF.mp3`\n\n"
                "Configure each option separately."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔤 Prefix", callback_data="setting_prefix"),
                    InlineKeyboardButton("🔣 Suffix", callback_data="setting_suffix")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_header_footer":
            settings = await get_user_settings(user_id)
            header = settings.get("caption_header", "")
            footer = settings.get("caption_footer", "")
            
            text = (
                "📄 **Caption Header & Footer Setting**\n\n"
                f"**Header:** `{header[:30] + '...' if len(header) > 30 else header or 'None'}`\n"
                f"**Footer:** `{footer[:30] + '...' if len(footer) > 30 else footer or 'None'}`\n\n"
                "**What it does:**\n"
                "• Header: Text added at top of caption\n"
                "• Footer: Text added at bottom of caption\n\n"
                "**Examples:**\n"
                "• Header: `🎵 Music Bot`\n"
                "• Footer: `Converted by @AudioBot`\n\n"
                "Configure each option separately."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📄 Header", callback_data="setting_header"),
                    InlineKeyboardButton("📑 Footer", callback_data="setting_footer")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="setting_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_view":
            settings = await get_user_settings(user_id)
            
            text = (
                "📋 **Current Settings**\n\n"
                f"**🎵 Compression:** `{'✅ ON' if settings['compression'] else '❌ OFF'}`\n"
                f"**📦 Upload Type:** `{settings['upload_type'].title()}`\n"
                f"**🖼 Thumbnail:** `{settings['thumb_url'] or 'Not Set'}`\n"
                f"**📝 Rename Format:** `{settings['rename_format']}`\n"
                f"**🗑 Remove Text:** `{settings['remove_text'] or 'Not Set'}`\n"
                f"**🔄 Replace Text:** `{settings['replace_text']} → {settings['replace_with']}`\n"
                f"**🔤 Prefix:** `{settings['prefix'] or 'None'}`\n"
                f"**🔣 Suffix:** `{settings['suffix'] or 'None'}`\n"
                f"**📄 Caption Header:** `{settings['caption_header'] or 'None'}`\n"
                f"**📑 Caption Footer:** `{settings['caption_footer'] or 'None'}`\n\n"
                "Use the buttons below to modify settings."
            )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Edit Settings", callback_data="setting_back")],
                [InlineKeyboardButton("❌ Close", callback_data="setting_close")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_reset":
            text = (
                "🔄 **Reset All Settings**\n\n"
                "⚠️ **Warning:** This will reset ALL your settings to default values!\n\n"
                "**Default Values:**\n"
                "• Compression: ✅ ON\n"
                "• Upload Type: Audio\n"
                "• Thumbnail: Not Set\n"
                "• Rename Format: {original}\n"
                "• All text modifications: Cleared\n\n"
                "Are you sure you want to continue?"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Reset", callback_data="confirm_reset"),
                    InlineKeyboardButton("❌ No, Cancel", callback_data="setting_back")
                ]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "setting_back":
            # Go back to main settings menu
            await settings_command(_, query.message)
            await query.answer()
            return
            
        elif data == "setting_close":
            await query.message.delete()
            await query.answer("Settings closed")
            return
            
        # ================= SETTING TOGGLES =================
        elif data.startswith("toggle_compression_"):
            value = data.split("_")[-1] == "true"
            await update_user_settings(user_id, {"compression": value})
            await query.answer(f"Compression {'enabled' if value else 'disabled'}!")
            await setting_compression(_, query)
            
        elif data.startswith("set_upload_"):
            upload_type = data.split("_")[-1]
            await update_user_settings(user_id, {"upload_type": upload_type})
            await query.answer(f"Upload type set to {upload_type}!")
            await setting_upload_type(_, query)
            
        elif data == "add_thumb":
            USER_STATES[user_id] = {"action": "set_thumb"}
            await query.message.edit(
                "🖼 **Set Thumbnail URL**\n\n"
                "Please send me a direct image URL (JPG/PNG):\n\n"
                "**Example:**\n"
                "`https://example.com/image.jpg`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_thumb")]
                ])
            )
            
        elif data == "view_thumb":
            settings = await get_user_settings(user_id)
            thumb_url = settings.get("thumb_url", "")
            if thumb_url:
                await query.answer(f"Thumbnail URL: {thumb_url}", show_alert=True)
            else:
                await query.answer("No thumbnail set!", show_alert=True)
                
        elif data == "remove_thumb":
            await update_user_settings(user_id, {"thumb_url": ""})
            await query.answer("Thumbnail removed!")
            await setting_thumb(_, query)
            
        elif data == "add_rename":
            USER_STATES[user_id] = {"action": "set_rename"}
            await query.message.edit(
                "📝 **Set Rename Format**\n\n"
                "Please send me the new rename format:\n\n"
                "**Variables:**\n"
                "• `{original}` - Original filename\n\n"
                "**Examples:**\n"
                "• `MyAudio_{original}`\n"
                "• `{original}_converted`\n"
                "• `Custom_Name.mp3`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_rename")]
                ])
            )
            
        elif data == "view_rename":
            settings = await get_user_settings(user_id)
            rename_format = settings.get("rename_format", "{original}")
            await query.answer(f"Rename format: {rename_format}", show_alert=True)
            
        elif data == "remove_rename":
            await update_user_settings(user_id, {"rename_format": "{original}"})
            await query.answer("Rename format reset!")
            await setting_rename(_, query)
            
        elif data == "add_remove_text":
            USER_STATES[user_id] = {"action": "set_remove_text"}
            await query.message.edit(
                "🗑 **Set Text to Remove**\n\n"
                "Please send me the text to remove from filenames:\n\n"
                "**Example:**\n"
                "• Text: `[Official]`\n"
                "• Result: `[Official] Song.mp3` → `Song.mp3`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_remove_text")]
                ])
            )
            
        elif data == "view_remove_text":
            settings = await get_user_settings(user_id)
            remove_text = settings.get("remove_text", "")
            if remove_text:
                await query.answer(f"Text to remove: {remove_text}", show_alert=True)
            else:
                await query.answer("No text to remove!", show_alert=True)
                
        elif data == "remove_remove_text":
            await update_user_settings(user_id, {"remove_text": ""})
            await query.answer("Remove text cleared!")
            await setting_remove_text(_, query)
            
        elif data == "add_replace_text":
            USER_STATES[user_id] = {"action": "set_replace_text"}
            await query.message.edit(
                "🔄 **Set Text Replacement**\n\n"
                "Please send me the text to replace and the replacement text,\n"
                "separated by a comma:\n\n"
                "**Format:** `old_text, new_text`\n\n"
                "**Example:**\n"
                "`old, new`\n"
                "Result: `old_song.mp3` → `new_song.mp3`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_replace_text")]
                ])
            )
            
        elif data == "view_replace_text":
            settings = await get_user_settings(user_id)
            replace_text = settings.get("replace_text", "")
            replace_with = settings.get("replace_with", "")
            if replace_text:
                await query.answer(f"Replace: {replace_text} → {replace_with}", show_alert=True)
            else:
                await query.answer("No replacement set!", show_alert=True)
                
        elif data == "remove_replace_text":
            await update_user_settings(user_id, {"replace_text": "", "replace_with": ""})
            await query.answer("Replacement cleared!")
            await setting_replace_text(_, query)
            
        elif data == "setting_prefix":
            USER_STATES[user_id] = {"action": "set_prefix"}
            await query.message.edit(
                "🔤 **Set Prefix**\n\n"
                "Please send me the prefix text to add to filenames:\n\n"
                "**Example:**\n"
                "• Prefix: `PRE_`\n"
                "• Result: `PRE_filename.mp3`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_prefix_suffix")]
                ])
            )
            
        elif data == "setting_suffix":
            USER_STATES[user_id] = {"action": "set_suffix"}
            await query.message.edit(
                "🔣 **Set Suffix**\n\n"
                "Please send me the suffix text to add to filenames:\n\n"
                "**Example:**\n"
                "• Suffix: `_SUF`\n"
                "• Result: `filename_SUF.mp3`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_prefix_suffix")]
                ])
            )
            
        elif data == "setting_header":
            USER_STATES[user_id] = {"action": "set_header"}
            await query.message.edit(
                "📄 **Set Caption Header**\n\n"
                "Please send me the header text for captions:\n\n"
                "**Example:**\n"
                "• Header: `🎵 Music Bot`\n"
                "• Result: Header appears at top of caption\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_header_footer")]
                ])
            )
            
        elif data == "setting_footer":
            USER_STATES[user_id] = {"action": "set_footer"}
            await query.message.edit(
                "📑 **Set Caption Footer**\n\n"
                "Please send me the footer text for captions:\n\n"
                "**Example:**\n"
                "• Footer: `Converted by @AudioBot`\n"
                "• Result: Footer appears at bottom of caption\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="setting_header_footer")]
                ])
            )
            
        elif data == "confirm_reset":
            # Reset to default settings
            default_settings = {
                "compression": True,
                "upload_type": "audio",
                "thumb_url": "",
                "rename_format": "{original}",
                "remove_text": "",
                "replace_text": "",
                "replace_with": "",
                "suffix": "",
                "prefix": "",
                "caption_header": "",
                "caption_footer": ""
            }
            await update_user_settings(user_id, default_settings)
            await query.answer("All settings have been reset to default!")
            await settings_command(_, query.message)
            
    except Exception as e:
        logger.error(f"Settings callback error: {e}")
        await query.answer("❌ An error occurred", show_alert=True)

# ================= ADMIN CALLBACKS =================
@app.on_callback_query(filters.regex(r"^admin_"))
async def admin_callback_handler(_, query: CallbackQuery):
    """Handle admin callbacks"""
    user_id = query.from_user.id
    
    if not await is_admin(user_id):
        await query.answer("❌ Access Denied! Admins only.", show_alert=True)
        return
    
    data = query.data
    
    try:
        if data == "admin_ban":
            USER_STATES[user_id] = {"action": "ban_user"}
            await query.message.edit(
                "🚫 **Ban User**\n\n"
                "Please enter the user ID to ban:\n\n"
                "**Format:** Just send the user ID as a number.\n"
                "**Example:** `123456789`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
                ])
            )
            
        elif data == "admin_unban":
            USER_STATES[user_id] = {"action": "unban_user"}
            await query.message.edit(
                "✅ **Unban User**\n\n"
                "Please enter the user ID to unban:\n\n"
                "**Format:** Just send the user ID as a number.\n"
                "**Example:** `123456789`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
                ])
            )
            
        elif data == "admin_add_premium":
            USER_STATES[user_id] = {"action": "add_premium"}
            await query.message.edit(
                "👑 **Add Premium User**\n\n"
                "Please enter the user ID to add to premium:\n\n"
                "**Format:** Just send the user ID as a number.\n"
                "**Example:** `123456789`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
                ])
            )
            
        elif data == "admin_remove_premium":
            USER_STATES[user_id] = {"action": "remove_premium"}
            await query.message.edit(
                "👤 **Remove Premium User**\n\n"
                "Please enter the user ID to remove from premium:\n\n"
                "**Format:** Just send the user ID as a number.\n"
                "**Example:** `123456789`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_back")]
                ])
            )
            
        elif data == "admin_view_premium":
            premium_users = await get_premium_users()
            
            if not premium_users:
                text = "📋 **Premium Users**\n\nNo premium users found."
            else:
                text = "📋 **Premium Users**\n\n"
                for i, user in enumerate(premium_users[:50], 1):  # Limit to 50
                    user_id = user.get("user_id")
                    premium_at = user.get("premium_at", "")
                    if premium_at and isinstance(premium_at, datetime):
                        date_str = premium_at.strftime("%Y-%m-%d")
                    else:
                        date_str = "Unknown"
                    text += f"{i}. `{user_id}` - Since: {date_str}\n"
                
                if len(premium_users) > 50:
                    text += f"\n... and {len(premium_users) - 50} more users"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "admin_restart":
            await query.message.edit(
                "🔁 **Restart Bot**\n\n"
                "⚠️ **Warning:** This will restart the bot!\n"
                "All ongoing processes will be interrupted.\n\n"
                "Are you sure you want to restart the bot?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Yes, Restart", callback_data="confirm_restart"),
                        InlineKeyboardButton("❌ No, Cancel", callback_data="admin_back")
                    ]
                ])
            )
            
        elif data == "admin_log_channel":
            log_channel = await get_log_channel()
            
            text = (
                "📢 **Log Channel Settings**\n\n"
                f"**Current Log Channel:** `{log_channel or 'Not Set'}`\n\n"
                "**What it does:**\n"
                "• Logs important bot events\n"
                "• Tracks user actions\n"
                "• Monitors system status\n\n"
                "Select an option below:"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ Add Channel", callback_data="add_log_channel"),
                    InlineKeyboardButton("👁 View Channel", callback_data="view_log_channel"),
                    InlineKeyboardButton("🗑 Remove Channel", callback_data="remove_log_channel")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "admin_free_mode":
            free_mode = await get_free_mode()
            
            text = (
                "🌐 **Free Mode Setting**\n\n"
                f"**Current Status:** `{'✅ ON' if free_mode else '❌ OFF'}`\n\n"
                "**What it does:**\n"
                "• ✅ ON: All users have unlimited access (no premium restrictions)\n"
                "• ❌ OFF: Only premium users can use certain features\n\n"
                "**Note:** When OFF, non-premium users will be restricted."
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Enable", callback_data="toggle_free_mode_true"),
                    InlineKeyboardButton("❌ Disable", callback_data="toggle_free_mode_false")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "admin_stats":
            stats = await get_system_stats()
            
            if not stats:
                text = "❌ Could not fetch statistics"
            else:
                text = (
                    f"📊 **Detailed Statistics**\n\n"
                    f"**🤖 Bot Info:**\n"
                    f"• ⏱ Uptime: `{format_uptime(int(stats['uptime']))}`\n"
                    f"• 🧠 Process RAM: `{stats['process_memory']:.2f} MB`\n"
                    f"• 👥 Active Workers: `{stats['active_workers']}`\n\n"
                    
                    f"**👥 User Stats:**\n"
                    f"• 👤 Total Users: `{stats['total_users']}`\n"
                    f"• 📁 Queued Files: `{stats['total_queued']}`\n"
                    f"• 👑 Premium Users: `{stats['total_premium']}`\n"
                    f"• 🚫 Banned Users: `{stats['total_banned']}`\n"
                    f"• 🌐 Free Mode: `{'✅ ON' if stats['free_mode'] else '❌ OFF'}`\n\n"
                    
                    f"**💻 System:**\n"
                    f"• 🖥 CPU Usage: `{stats['cpu']:.1f}%`\n"
                    f"• 🧮 Memory: `{stats['memory']:.1f}%` "
                    f"(`{stats['memory_used']:.2f}/{stats['memory_total']:.2f} GB`)\n"
                    f"• 💾 Disk: `{stats['disk']:.1f}%` "
                    f"(`{stats['disk_used']:.2f}/{stats['disk_total']:.2f} GB`)\n"
                    f"• 📡 Network: ↑`{human_readable_size(stats['bytes_sent'])}` "
                    f"↓`{human_readable_size(stats['bytes_recv'])}`\n\n"
                    
                    f"**🗄 Database:**\n"
                    f"• 📊 MongoDB: {stats['mongo_status']}\n"
                    f"• 🐍 Python: `{platform.python_version()}`\n"
                    f"• 🕐 Updated: `{datetime.fromisoformat(stats['timestamp']).strftime('%H:%M:%S')}`"
                )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
            ])
            
            await query.message.edit(text, reply_markup=keyboard)
            
        elif data == "admin_back":
            await admin_command(_, query.message)
            await query.answer()
            return
            
        elif data == "admin_close":
            await query.message.delete()
            await query.answer("Admin panel closed")
            return
            
        # ================= ADMIN ACTIONS =================
        elif data == "confirm_restart":
            await query.message.edit("🔄 **Restarting bot...**\n\nPlease wait a few moments.")
            await query.answer()
            
            # Send restart notification
            try:
                await app.send_message(
                    OWNER_ID,
                    f"🔄 **Bot Restart Initiated**\n\n"
                    f"👤 By: {query.from_user.mention}\n"
                    f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            except:
                pass
            
            # Restart the bot
            os.execv(sys.executable, [sys.executable] + sys.argv)
            
        elif data == "add_log_channel":
            USER_STATES[user_id] = {"action": "set_log_channel"}
            await query.message.edit(
                "📢 **Set Log Channel**\n\n"
                "Please enter the channel ID to set as log channel:\n\n"
                "**Format:** Just send the channel ID as a number.\n"
                "**Note:** Must start with -100 for supergroups\n"
                "**Example:** `-1001234567890`\n\n"
                "Click ❌ Cancel to go back.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_log_channel")]
                ])
            )
            
        elif data == "view_log_channel":
            log_channel = await get_log_channel()
            if log_channel:
                await query.answer(f"Log Channel: {log_channel}", show_alert=True)
            else:
                await query.answer("No log channel set!", show_alert=True)
                
        elif data == "remove_log_channel":
            await admin_col.delete_one({"key": "log_channel"})
            await query.answer("Log channel removed!")
            await admin_log_channel(_, query)
            
        elif data.startswith("toggle_free_mode_"):
            value = data.split("_")[-1] == "true"
            await admin_col.update_one(
                {"key": "free_mode"},
                {"$set": {"value": value, "updated_at": datetime.now()}},
                upsert=True
            )
            await query.answer(f"Free mode {'enabled' if value else 'disabled'}!")
            await admin_free_mode(_, query)
            
    except Exception as e:
        logger.error(f"Admin callback error: {e}")
        await query.answer("❌ An error occurred", show_alert=True)

# ================= MESSAGE HANDLER FOR STATES =================
@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(["start", "stats", "admin", "settings", "queue", "cancel", "help"])
)
async def state_message_handler(_, m: Message):
    """Handle messages for conversation states"""
    user_id = m.from_user.id
    
    if user_id not in USER_STATES:
        return
    
    state = USER_STATES[user_id]
    action = state.get("action")
    
    try:
        if action == "set_thumb":
            thumb_url = m.text.strip()
            
            # Basic URL validation
            if not (thumb_url.startswith("http://") or thumb_url.startswith("https://")):
                await m.reply("❌ Invalid URL! Please send a valid HTTP/HTTPS URL.")
                return
            
            await update_user_settings(user_id, {"thumb_url": thumb_url})
            await m.reply(f"✅ Thumbnail URL set to:\n`{thumb_url}`")
            del USER_STATES[user_id]
            
        elif action == "set_rename":
            rename_format = m.text.strip()
            await update_user_settings(user_id, {"rename_format": rename_format})
            await m.reply(f"✅ Rename format set to:\n`{rename_format}`")
            del USER_STATES[user_id]
            
        elif action == "set_remove_text":
            remove_text = m.text.strip()
            await update_user_settings(user_id, {"remove_text": remove_text})
            await m.reply(f"✅ Text to remove set to:\n`{remove_text}`")
            del USER_STATES[user_id]
            
        elif action == "set_replace_text":
            text = m.text.strip()
            if "," not in text:
                await m.reply("❌ Invalid format! Please use: `old_text, new_text`")
                return
            
            replace_text, replace_with = [t.strip() for t in text.split(",", 1)]
            await update_user_settings(user_id, {
                "replace_text": replace_text,
                "replace_with": replace_with
            })
            await m.reply(f"✅ Replacement set:\n`{replace_text}` → `{replace_with}`")
            del USER_STATES[user_id]
            
        elif action == "set_prefix":
            prefix = m.text.strip()
            await update_user_settings(user_id, {"prefix": prefix})
            await m.reply(f"✅ Prefix set to:\n`{prefix}`")
            del USER_STATES[user_id]
            
        elif action == "set_suffix":
            suffix = m.text.strip()
            await update_user_settings(user_id, {"suffix": suffix})
            await m.reply(f"✅ Suffix set to:\n`{suffix}`")
            del USER_STATES[user_id]
            
        elif action == "set_header":
            header = m.text.strip()
            await update_user_settings(user_id, {"caption_header": header})
            await m.reply(f"✅ Caption header set to:\n`{header}`")
            del USER_STATES[user_id]
            
        elif action == "set_footer":
            footer = m.text.strip()
            await update_user_settings(user_id, {"caption_footer": footer})
            await m.reply(f"✅ Caption footer set to:\n`{footer}`")
            del USER_STATES[user_id]
            
        # Admin actions
        elif action == "ban_user":
            if not await is_admin(user_id):
                del USER_STATES[user_id]
                return
            
            try:
                target_id = int(m.text.strip())
                await ban_user(target_id, user_id)
                await m.reply(f"✅ User `{target_id}` has been banned.")
                
                # Log to channel
                log_channel = await get_log_channel()
                if log_channel:
                    await app.send_message(
                        log_channel,
                        f"🚫 **User Banned**\n\n"
                        f"👤 User ID: `{target_id}`\n"
                        f"👮 Banned by: {m.from_user.mention}\n"
                        f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                
            except ValueError:
                await m.reply("❌ Invalid user ID! Please send a numeric ID.")
                return
            finally:
                del USER_STATES[user_id]
                
        elif action == "unban_user":
            if not await is_admin(user_id):
                del USER_STATES[user_id]
                return
            
            try:
                target_id = int(m.text.strip())
                await unban_user(target_id)
                await m.reply(f"✅ User `{target_id}` has been unbanned.")
                
                # Log to channel
                log_channel = await get_log_channel()
                if log_channel:
                    await app.send_message(
                        log_channel,
                        f"✅ **User Unbanned**\n\n"
                        f"👤 User ID: `{target_id}`\n"
                        f"👮 Action by: {m.from_user.mention}\n"
                        f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                
            except ValueError:
                await m.reply("❌ Invalid user ID! Please send a numeric ID.")
                return
            finally:
                del USER_STATES[user_id]
                
        elif action == "add_premium":
            if not await is_admin(user_id):
                del USER_STATES[user_id]
                return
            
            try:
                target_id = int(m.text.strip())
                await add_premium(target_id, user_id)
                await m.reply(f"✅ User `{target_id}` added to premium users.")
                
                # Notify user if possible
                try:
                    await app.send_message(
                        target_id,
                        "🎉 **You've been granted Premium Access!**\n\n"
                        "You now have access to all premium features.\n"
                        "Thank you for using our bot!"
                    )
                except:
                    pass
                
                # Log to channel
                log_channel = await get_log_channel()
                if log_channel:
                    await app.send_message(
                        log_channel,
                        f"👑 **Premium User Added**\n\n"
                        f"👤 User ID: `{target_id}`\n"
                        f"👮 Added by: {m.from_user.mention}\n"
                        f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                
            except ValueError:
                await m.reply("❌ Invalid user ID! Please send a numeric ID.")
                return
            finally:
                del USER_STATES[user_id]
                
        elif action == "remove_premium":
            if not await is_admin(user_id):
                del USER_STATES[user_id]
                return
            
            try:
                target_id = int(m.text.strip())
                await remove_premium(target_id)
                await m.reply(f"✅ User `{target_id}` removed from premium users.")
                
                # Log to channel
                log_channel = await get_log_channel()
                if log_channel:
                    await app.send_message(
                        log_channel,
                        f"👤 **Premium User Removed**\n\n"
                        f"👤 User ID: `{target_id}`\n"
                        f"👮 Removed by: {m.from_user.mention}\n"
                        f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                
            except ValueError:
                await m.reply("❌ Invalid user ID! Please send a numeric ID.")
                return
            finally:
                del USER_STATES[user_id]
                
        elif action == "set_log_channel":
            if not await is_admin(user_id):
                del USER_STATES[user_id]
                return
            
            try:
                channel_id = int(m.text.strip())
                await set_log_channel(channel_id)
                await m.reply(f"✅ Log channel set to:\n`{channel_id}`")
                
                # Test message to log channel
                try:
                    await app.send_message(
                        channel_id,
                        "📢 **Log Channel Set Successfully!**\n\n"
                        "This channel will now receive bot logs and notifications."
                    )
                except Exception as e:
                    await m.reply(f"⚠️ Could not send test message: {e}")
                
            except ValueError:
                await m.reply("❌ Invalid channel ID! Please send a numeric ID.")
                return
            finally:
                del USER_STATES[user_id]
                
    except Exception as e:
        logger.error(f"State handler error: {e}")
        await m.reply("❌ An error occurred while processing your request.")
        if user_id in USER_STATES:
            del USER_STATES[user_id]

# ================= EXISTING COMMANDS =================
@app.on_message(filters.command("start"))
async def start(_, m: Message):
    await m.reply(
        "🎵 **Audio Queue Bot**\n\n"
        "📥 Send me audio files or documents\n"
        "🔧 I'll process them with your settings\n"
        "📊 Each user has their own queue\n"
        "🔄 Files processed one by one\n\n"
        "**Commands:**\n"
        "• /settings - Customize processing settings\n"
        "• /stats - Show bot statistics\n"
        "• /cancel - Cancel current queue\n"
        "• /queue - Show your queue status\n\n"
        "**Features:**\n"
        "✅ Custom compression settings\n"
        "✅ File renaming & formatting\n"
        "✅ Custom thumbnails\n"
        "✅ Progress tracking\n"
        "✅ Queue management\n"
        "✅ Cancel any time"
    )

@app.on_message(filters.command("stats"))
async def stats(_, m: Message):
    msg = await m.reply("📊 Collecting system statistics...")
    
    try:
        stats_data = await get_system_stats()
        
        if not stats_data:
            await msg.edit("❌ Failed to collect statistics")
            return
        
        # Format stats message
        stats_text = (
            f"📊 **System Statistics**\n\n"
            f"**🤖 Bot Info:**\n"
            f"• ⏱ Uptime: `{format_uptime(int(stats_data['uptime']))}`\n"
            f"• 🧠 Process RAM: `{stats_data['process_memory']:.2f} MB`\n"
            f"• 👥 Active Workers: `{stats_data['active_workers']}`\n"
            f"• 👤 Total Users: `{stats_data['total_users']}`\n"
            f"• 📁 Queued Files: `{stats_data['total_queued']}`\n\n"
            
            f"**💻 System:**\n"
            f"• 🖥 CPU Usage: `{stats_data['cpu']:.1f}%`\n"
            f"• 🧮 Memory: `{stats_data['memory']:.1f}%` "
            f"(`{stats_data['memory_used']:.2f}/{stats_data['memory_total']:.2f} GB`)\n"
            f"• 💾 Disk: `{stats_data['disk']:.1f}%` "
            f"(`{stats_data['disk_used']:.2f}/{stats_data['disk_total']:.2f} GB`)\n"
            f"• 📡 Network: ↑`{human_readable_size(stats_data['bytes_sent'])}` "
            f"↓`{human_readable_size(stats_data['bytes_recv'])}`\n\n"
            
            f"**🗄 Database:**\n"
            f"• 📊 MongoDB: {stats_data['mongo_status']}\n"
            f"• 🐍 Python: `{platform.python_version()}`\n"
            f"• 🕐 Updated: `{datetime.fromisoformat(stats_data['timestamp']).strftime('%H:%M:%S')}`"
        )
        
        await msg.edit(stats_text)
        
    except Exception as e:
        logger.error(f"Stats command error: {e}")
        await msg.edit("❌ Error collecting statistics")

@app.on_message(filters.command("cancel"))
async def cancel_cmd(_, m: Message):
    user_id = m.from_user.id
    
    await cancel_user_queue(user_id, cancel_all=True)
    
    await m.reply(
        "✅ **Queue Cancelled**\n\n"
        "All your pending files have been removed from the queue.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Start Over", callback_data="start_over")]
        ])
    )

@app.on_message(filters.command("queue"))
async def queue_cmd(_, m: Message):
    user_id = m.from_user.id
    queue_status = await get_queue_status(user_id)
    
    if queue_status["total"] == 0:
        await m.reply("📭 **Your queue is empty**\n\nSend me files to add them to your queue!")
        return
    
    queue_list = ""
    for i, task in enumerate(queue_status["queue"][:5], 1):
        name = task.get("file_name", "Unknown")
        if len(name) > 30:
            name = name[:27] + "..."
        queue_list += f"{i}. `{name}`\n"
    
    if queue_status["total"] > 5:
        queue_list += f"\n... and {queue_status['total'] - 5} more files"
    
    await m.reply(
        f"📋 **Your Queue Status**\n\n"
        f"📊 **Total Files:** {queue_status['total']}\n"
        f"🎯 **Current Position:** {queue_status['position']}\n"
        f"🔄 **Status:** {'Processing' if queue_status['active'] else 'Waiting'}\n\n"
        f"**Next files:**\n{queue_list}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Cancel All", callback_data=f"cancel_all_{user_id}"),
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh_queue")
            ]
        ])
    )

@app.on_message(filters.audio | filters.document)
async def audio_handler(_, m: Message):
    user_id = m.from_user.id
    
    # Check if user is banned
    if await is_banned(user_id):
        await m.reply("❌ **You are banned from using this bot!**")
        return
    
    # Check premium status if free mode is off
    free_mode = await get_free_mode()
    if not free_mode and not await is_premium(user_id):
        await m.reply(
            "⚠️ **Premium Required**\n\n"
            "Free mode is currently disabled. You need premium to use this bot.\n"
            "Contact the bot owner for premium access."
        )
        return
    
    # Check file type
    if m.audio:
        file = m.audio
        file_name = file.file_name or f"audio_{file.file_unique_id}.mp3"
    elif m.document:
        file = m.document
        # Check if it's an audio file
        if file.mime_type and not file.mime_type.startswith('audio/'):
            await m.reply("❌ Please send audio files only!")
            return
        file_name = file.file_name or f"document_{file.file_unique_id}.mp3"
    else:
        return
    
    # Check file size (limit to 2GB for Telegram)
    if file.file_size and file.file_size > 2 * 1024 * 1024 * 1024:
        await m.reply("❌ File size too large! Maximum size is 2GB")
        return
    
    # Get user settings for filename formatting
    settings = await get_user_settings(user_id)
    formatted_filename = await apply_filename_format(file_name, settings)
    
    # Create task
    task = {
        "file_id": file.file_id,
        "file_name": file_name,
        "formatted_name": formatted_filename,
        "file_size": file.file_size,
        "added_at": datetime.now().isoformat(),
        "user_id": user_id
    }
    
    # Add to queue
    await add_queue(user_id, task)
    
    # Get queue status
    queue_status = await get_queue_status(user_id)
    
    # Create response with buttons
    await m.reply(
        f"✅ **Added to Queue**\n\n"
        f"📄 **File:** `{formatted_filename}`\n"
        f"📊 **Size:** {human_readable_size(file.file_size) if file.file_size else 'Unknown'}\n"
        f"📋 **Position in Queue:** {queue_status['total']}\n"
        f"⚙️ **Settings:** {'Compress' if settings['compression'] else 'No Compress'} | {settings['upload_type'].title()}\n"
        f"⏳ **Estimated Wait:** ~{queue_status['total'] * 2} minutes\n\n"
        "Files are processed one by one automatically.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="setting_back"),
                InlineKeyboardButton("📋 View Queue", callback_data="view_queue")
            ]
        ])
    )
    
    # Start worker if not already running
    if not queue_status.get("active") and user_id not in USER_WORKERS:
        asyncio.create_task(worker(user_id, m.chat.id))
    elif queue_status.get("active"):
        await m.reply("⚠️ Your queue is already being processed. New files will be added to the end.")

# ================= EXISTING CALLBACKS =================
@app.on_callback_query(filters.regex(r"^(cancel_|refresh_queue|view_queue|start_over)"))
async def existing_callbacks_handler(_, query: CallbackQuery):
    """Handle existing callbacks"""
    user_id = query.from_user.id
    data = query.data
    
    try:
        if data.startswith("cancel_current_"):
            target_user = int(data.split("_")[2])
            if target_user != user_id:
                await query.answer("❌ This is not your queue!", show_alert=True)
                return
            
            await cancel_user_queue(user_id, cancel_all=False)
            await query.answer("⏸ Current file cancelled")
            await query.message.edit(
                "⏸ **Current file cancelled**\n\n"
                "Moving to next file in queue...",
                reply_markup=None
            )
            
        elif data.startswith("cancel_all_"):
            target_user = int(data.split("_")[2])
            if target_user != user_id:
                await query.answer("❌ This is not your queue!", show_alert=True)
                return
            
            await cancel_user_queue(user_id, cancel_all=True)
            await query.answer("🗑 All files cancelled")
            await query.message.edit(
                "✅ **Queue Cleared**\n\n"
                "All your pending files have been removed.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Start Over", callback_data="start_over")]
                ])
            )
            
        elif data == "view_queue":
            queue_status = await get_queue_status(user_id)
            
            if queue_status["total"] == 0:
                await query.answer("Your queue is empty!", show_alert=True)
                return
            
            queue_list = ""
            for i, task in enumerate(queue_status["queue"][:5], 1):
                name = task.get("file_name", "Unknown")
                if len(name) > 30:
                    name = name[:27] + "..."
                queue_list += f"{i}. `{name}`\n"
            
            await query.message.edit(
                f"📋 **Your Queue**\n\n"
                f"📊 **Total:** {queue_status['total']} files\n"
                f"🎯 **Current:** Position {queue_status['position']}\n\n"
                f"**Next in line:**\n{queue_list}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌ Cancel All", callback_data=f"cancel_all_{user_id}"),
                        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_queue")
                    ]
                ])
            )
            await query.answer("✅ Queue updated")
            
        elif data == "refresh_queue":
            await query.answer("🔄 Refreshing...")
            queue_status = await get_queue_status(user_id)
            
            queue_list = ""
            for i, task in enumerate(queue_status["queue"][:5], 1):
                name = task.get("file_name", "Unknown")
                if len(name) > 30:
                    name = name[:27] + "..."
                queue_list += f"{i}. `{name}`\n"
            
            await query.message.edit(
                f"📋 **Your Queue** (Refreshed)\n\n"
                f"📊 **Total:** {queue_status['total']} files\n"
                f"🎯 **Current:** Position {queue_status['position']}\n"
                f"🔄 **Status:** {'Processing' if queue_status['active'] else 'Waiting'}\n\n"
                f"**Next in line:**\n{queue_list}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("❌ Cancel All", callback_data=f"cancel_all_{user_id}"),
                        InlineKeyboardButton("🔄 Refresh", callback_data="refresh_queue")
                    ]
                ])
            )
            
        elif data == "start_over":
            await query.message.edit(
                "🔄 **Ready to Start**\n\n"
                "Send me audio files to begin processing!",
                reply_markup=None
            )
            await query.answer("✅ Ready!")
            
    except Exception as e:
        logger.error(f"Existing callback error: {e}")
        await query.answer("❌ An error occurred", show_alert=True)

# ================= CLEANUP =================
async def cleanup_downloads():
    """Clean up old download files"""
    try:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_DIR):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                # Delete files older than 1 hour
                if now - os.path.getctime(filepath) > 3600:
                    os.remove(filepath)
        logger.info("Cleanup completed")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ================= START =================
def notify_owner():
    """Notify owner on restart"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": OWNER_ID,
                "text": f"✅ Audio Queue Bot Restarted\n\n"
                       f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"🐍 Python {platform.python_version()}\n"
                       f"🖥 {platform.system()} {platform.release()}\n"
                       f"👑 Admins: {len(ADMIN_IDS)}\n"
                       f"⚙️ New Features: Settings & Admin Panel"
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"Owner notification failed: {e}")

if __name__ == "__main__":
    import sys
    
    # Create required directories
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # Initialize database
    asyncio.get_event_loop().run_until_complete(initialize_db())
    
    # Notify owner
    notify_owner()
    
    # Schedule cleanup
    async def scheduled_cleanup():
        while True:
            await asyncio.sleep(3600)  # Run every hour
            await cleanup_downloads()
    
    # Start cleanup task
    asyncio.get_event_loop().create_task(scheduled_cleanup())
    
    logger.info("🎵 Audio Queue Bot Starting...")
    logger.info(f"👑 Admin IDs: {ADMIN_IDS}")
    app.run()
