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
from typing import Dict, List
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified

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
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://adam822728:iP9ESt5vyfwDRxNB@cluster0.r82vfuz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

DOWNLOAD_DIR = "downloads"
MAX_CONCURRENT_WORKERS = 1  # Process one file at a time per user
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BOT_START_TIME = time.time()
USER_WORKERS: Dict[int, asyncio.Task] = {}

# ================= BOT ====================
app = Client(
    "audio_queue_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
    max_concurrent_transmissions=5
)

# ================= DATABASE =================
try:
    mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo.server_info()  # Test connection
    db = mongo.audio_bot
    queue_col = db.queue
    stats_col = db.stats
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    exit(1)

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
        
        # Calculate percentage for progress bar
        percentage = (current / total * 100) if total > 0 else 0
        
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
            'mongo_status': mongo_status,
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting system stats: {e}")
        return None

# ================= AUDIO ==================
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
        # Check if already active
        data = await queue_col.find_one({"_id": user_id})
        if data and data.get("active"):
            logger.info(f"Worker already active for user {user_id}")
            return
        
        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"active": True, "cancel": False}}
        )
        
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
            file_name = task.get("file_name", "Unknown")
            file_id = task.get("file_id")
            
            # Create status message with buttons
            status_msg = await app.send_message(
                chat_id,
                f"📥 **Downloading:** `{file_name}`\n"
                f"📊 **Queue:** 1/{total}\n"
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
                download_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(start_time)}_{file_name}")
                
                await status_msg.edit(
                    f"📥 **Downloading:** `{file_name}`\n"
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
                
                # Compress
                output_path = input_path.rsplit('.', 1)[0] + "_compressed.mp3"
                
                await status_msg.edit(
                    f"🎛 **Compressing:** `{file_name}`\n"
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
                
                # Get audio info
                duration = await get_audio_duration(output_path)
                file_size = os.path.getsize(output_path)
                
                # Upload
                await status_msg.edit(
                    f"📤 **Uploading:** `{file_name}`\n"
                    f"📊 **Queue:** 1/{total}\n"
                    f"📏 **Size:** {human_readable_size(file_size)}\n"
                    f"⏱ **Duration:** {duration}s\n"
                    f"⏳ **Status:** Uploading...",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                            InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
                        ]
                    ])
                )
                
                upload_start = time.time()
                await app.send_audio(
                    chat_id,
                    audio=output_path,
                    duration=duration,
                    title=os.path.splitext(file_name)[0],
                    performer="Audio Queue Bot",
                    progress=progress,
                    progress_args=(status_msg, upload_start, "Uploading", f"Queue: 1/{total}")
                )
                
                # Cleanup
                try:
                    os.remove(input_path)
                    os.remove(output_path)
                except:
                    pass
                
                # Update status
                elapsed = time.time() - start_time
                await status_msg.edit(
                    f"✅ **Completed:** `{file_name}`\n"
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
                    f"❌ **Failed:** `{file_name}`\n"
                    f"📊 **Queue:** 1/{total}\n"
                    f"💥 **Error:** {str(e)[:100]}...\n"
                    f"❌ **Status:** Processing failed"
                )
                
                # Cleanup on error
                try:
                    if 'input_path' in locals() and os.path.exists(input_path):
                        os.remove(input_path)
                    if 'output_path' in locals() and os.path.exists(output_path):
                        os.remove(output_path)
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

# ================= COMMANDS =================
@app.on_message(filters.command("start"))
async def start(_, m: Message):
    await m.reply(
        "🎵 **Audio Queue Bot**\n\n"
        "📥 Send me audio files or documents\n"
        "🔧 I'll compress and send them back\n"
        "📊 Each user has their own queue\n"
        "🔄 Files processed one by one\n\n"
        "**Commands:**\n"
        "• /stats - Show bot statistics\n"
        "• /cancel - Cancel current queue\n"
        "• /queue - Show your queue status\n\n"
        "**Features:**\n"
        "✅ Automatic compression\n"
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
    
    # Create task
    task = {
        "file_id": file.file_id,
        "file_name": file_name,
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
        f"📄 **File:** `{file_name}`\n"
        f"📊 **Size:** {human_readable_size(file.file_size) if file.file_size else 'Unknown'}\n"
        f"📋 **Position in Queue:** {queue_status['total']}\n"
        f"⏳ **Estimated Wait:** ~{queue_status['total'] * 2} minutes\n\n"
        f"Files are processed one by one automatically.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Cancel Current", callback_data=f"cancel_current_{user_id}"),
                InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}")
            ],
            [
                InlineKeyboardButton("📋 View Queue", callback_data="view_queue"),
                InlineKeyboardButton("📊 Stats", callback_data="show_stats")
            ]
        ])
    )
    
    # Start worker if not already running
    if not queue_status.get("active") and user_id not in USER_WORKERS:
        asyncio.create_task(worker(user_id, m.chat.id))
    elif queue_status.get("active"):
        await m.reply("⚠️ Your queue is already being processed. New files will be added to the end.")

# ================= CALLBACK QUERIES =================
@app.on_callback_query()
async def callback_handler(_, query):
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
            
        elif data == "show_stats":
            stats_data = await get_system_stats()
            
            if stats_data:
                stats_text = (
                    f"📊 **Quick Stats**\n\n"
                    f"• 👤 Your Queue: `{await get_queue_status(user_id)['total']}` files\n"
                    f"• 🖥 CPU: `{stats_data['cpu']:.1f}%`\n"
                    f"• 🧮 Memory: `{stats_data['memory']:.1f}%`\n"
                    f"• 💾 Disk: `{stats_data['disk']:.1f}%`\n"
                    f"• ⏱ Uptime: `{format_uptime(int(stats_data['uptime']))}`\n\n"
                    f"Use /stats for detailed information"
                )
                await query.message.edit(stats_text, reply_markup=None)
                await query.answer("📊 Stats updated")
            else:
                await query.answer("❌ Could not fetch stats", show_alert=True)
                
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
        logger.error(f"Callback error: {e}")
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
                       f"🖥 {platform.system()} {platform.release()}"
            },
            timeout=10
        )
    except Exception as e:
        logger.error(f"Owner notification failed: {e}")

if __name__ == "__main__":
    # Create required directories
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
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
    app.run()
