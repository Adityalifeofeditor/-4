import os
import time
import asyncio
import logging
import json
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import ffmpeg
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from motor.motor_asyncio import AsyncIOMotorClient

# ───────────────── CONFIG ─────────────────
API_ID = int(os.getenv("API_ID", 27169529))
API_HASH = os.getenv("API_HASH", "5d67602a4e0bbfabe669c0febeaf63b6")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8574806355:AAGOXL5nDpzMvaEdhBAR_4vw3N2NXDABuJs")
OWNER_ID = int(os.getenv("OWNER_ID", 6441347235))
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://adam822728:iP9ESt5vyfwDRxNB@cluster0.r82vfuz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
DB_NAME = "mp3bot"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GB
START_TIME = time.time()
BATCH_TIMEOUT = 35  # Increased slightly for safety

# ───────────────── LOGGING ─────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MP3Bot")

# ───────────────── DATABASE ─────────────────
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
queue_col = db.queue

# ───────────────── PYROGRAM ─────────────────
app = Client("mp3_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Globals
USER_WORKERS: Dict[int, asyncio.Task] = {}
MEDIA_GROUP_CACHE: Dict[str, List[Message]] = {}  # media_group_id -> list of Messages

@dataclass
class Task:
    _id: Any
    user_id: int
    chat_id: int
    file_id: str
    file_name: str
    batch_id: Optional[str] = None

# ───────────────── HELPERS ─────────────────
def human_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"

def human_size(bytes_: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_ < 1024:
            return f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"

async def get_duration(path: str) -> int:
    try:
        meta = extractMetadata(createParser(path))
        if meta and meta.has("duration"):
            return int(meta.get("duration").seconds)
    except Exception:
        pass
    try:
        probe = ffmpeg.probe(path)
        return int(float(probe['format']['duration']))
    except Exception:
        return 0

def progress_bar(current: int, total: int, width: int = 10) -> str:
    percent = current / total if total else 0
    filled = int(width * percent)
    return "█" * filled + "░" * (width - filled)

# ───────────────── QUEUE SYSTEM ─────────────────
async def add_to_queue(user_id: int, chat_id: int, file_id: str, file_name: str, batch_id: Optional[str] = None):
    inserted = await queue_col.insert_one({
        "user_id": user_id,
        "chat_id": chat_id,
        "file_id": file_id,
        "file_name": file_name,
        "batch_id": batch_id,
        "status": "pending",
        "created": datetime.utcnow()
    })
    logger.info(f"Added to queue: {file_name} for user {user_id}")
    return Task(_id=inserted.inserted_id, user_id=user_id, chat_id=chat_id, file_id=file_id, file_name=file_name, batch_id=batch_id)

async def get_next_batch(user_id: int):
    pipeline = [
        {"$match": {"user_id": user_id, "status": "pending"}},
        {"$group": {"_id": "$batch_id", "tasks": {"$push": "$$ROOT"}}},
        {"$project": {"_id": 0, "batch_id": "$_id", "tasks": 1}}
    ]
    batches = await queue_col.aggregate(pipeline).to_list(None)
    if not batches:
        return None
    batch = batches[0]
    for task_doc in batch["tasks"]:
        await queue_col.update_one({"_id": task_doc["_id"]}, {"$set": {"status": "processing"}})
    return [Task(**t, _id=t["_id"]) for t in batch["tasks"]]

async def mark_task_done(task_id: Any):
    await queue_col.update_one({"_id": task_id}, {"$set": {"status": "done"}})

async def mark_task_failed(task_id: Any, reason: str = "failed"):
    await queue_col.update_one({"_id": task_id}, {"$set": {"status": reason}})

async def clear_user_queue(user_id: int):
    await queue_col.update_many(
        {"user_id": user_id, "status": {"$in": ["pending", "processing"]}},
        {"$set": {"status": "canceled"}}
    )

# ───────────────── PROGRESS ─────────────────
async def download_progress(current: int, total: int, msg: Message, start_time: float):
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    text = (
        f"📥 Downloading...\n"
        f"`{progress_bar(current, total)}` {current/total*100:.1f}%\n"
        f"Speed: {human_size(speed)}/s | ETA: {human_time(eta)}"
    )
    try:
        await msg.edit(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)

async def upload_progress(current: int, total: int, msg: Message, start_time: float):
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    text = (
        f"📤 Uploading...\n"
        f"`{progress_bar(current, total)}` {current/total*100:.1f}%\n"
        f"Speed: {human_size(speed)}/s | ETA: {human_time(eta)}"
    )
    try:
        await msg.edit(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)

# ───────────────── USER WORKER ─────────────────
async def user_worker(user_id: int):
    logger.info(f"Worker started for user {user_id}")
    while True:
        batch = await get_next_batch(user_id)
        if not batch:
            await asyncio.sleep(2)
            continue

        chat_id = batch[0].chat_id
        batch_id = batch[0].batch_id or "single"
        total_files = len(batch)

        status_msg = await app.send_message(
            chat_id,
            f"🎧 Starting batch processing ({total_files} files)...\n"
            f"Use /cancel to stop the current batch.",
        )

        input_paths: Dict[Any, str] = {}
        download_start = time.time()

        # Parallel download
        download_tasks = []
        for task in batch:
            path = DOWNLOAD_DIR / task.file_name
            dl = asyncio.create_task(
                app.download_media(
                    task.file_id,
                    file_name=str(path),
                    progress=lambda c, t, dl_task=task: asyncio.create_task(
                        download_progress(c, t, status_msg, download_start)
                    )
                )
            )
            download_tasks.append((task, dl))

        results = await asyncio.gather(*[t[1] for t in download_tasks], return_exceptions=True)
        for (task, _), result in zip(download_tasks, results):
            if isinstance(result, Exception) or result is None:
                await status_msg.edit(status_msg.text.markdown + f"\n❌ Download failed: {task.file_name}")
                await mark_task_failed(task._id, "download_failed")
            else:
                input_paths[task._id] = str(DOWNLOAD_DIR / task.file_name)

        # Sequential compression + upload
        success = 0
        for task in batch:
            if task._id not in input_paths:
                continue

            input_path = input_paths[task._id]
            output_path = DOWNLOAD_DIR / f"mp3_{task.file_name.rsplit('.', 1)[0]}.mp3"

            proc_msg = await app.send_message(chat_id, f"🗜️ Compressing {task.file_name}...")

            try:
                # FFmpeg compression to 128k MP3
                stream = ffmpeg.input(input_path)
                stream = ffmpeg.output(
                    stream, str(output_path),
                    vn=None, acodec='libmp3lame', audio_bitrate='128k',
                    loglevel='error'
                )
                await asyncio.to_thread(ffmpeg.run, stream, overwrite_output=True)

                # Upload
                upload_start = time.time()
                await app.send_audio(
                    chat_id,
                    str(output_path),
                    performer="MP3 Bot 🎵",
                    title=task.file_name.rsplit('.', 1)[0],
                    duration=await get_duration(str(output_path)),
                    progress=lambda c, t: asyncio.create_task(
                        upload_progress(c, t, proc_msg, upload_start)
                    )
                )
                await proc_msg.edit(f"✅ {task.file_name} processed successfully")
                success += 1
                await mark_task_done(task._id)
            except Exception as e:
                logger.error(f"Error processing {task.file_name}: {str(e)}")
                await proc_msg.edit(f"❌ Failed: {task.file_name}")
                await mark_task_failed(task._id)

            # Cleanup files
            for p in [input_path, output_path]:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass

        await status_msg.edit(f"✅ Batch finished: {success}/{total_files} successful")

    USER_WORKERS.pop(user_id, None)
    logger.info(f"Worker stopped for user {user_id}")

# ───────────────── HANDLERS ─────────────────
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    await message.reply(
        "**👋 Welcome to MP3 Compressor Bot!**\n\n"
        "Send me one or multiple MP3 files (as an album/media group).\n"
        "I will compress them to 128kbps and send back as Telegram audio files.\n\n"
        "Use /cancel to cancel your current processing batch.\n"
        "Use /stats for bot statistics."
    )

@app.on_message(filters.command("stats"))
async def stats(client: Client, message: Message):
    uptime = time.time() - START_TIME
    pending = await queue_col.count_documents({"status": "pending"})
    processing = await queue_col.count_documents({"status": "processing"})
    active_workers = len(USER_WORKERS)
    await message.reply(
        f"📊 **Bot Stats**\n\n"
        f"⏱️ Uptime: {human_time(uptime)}\n"
        f"👥 Active Workers: {active_workers}\n"
        f"📦 Pending: {pending}\n"
        f"⚙️ Processing: {processing}"
    )

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    await clear_user_queue(user_id)
    await message.reply("⏹️ Your queue has been canceled.")

@app.on_message(filters.command("queue") & filters.user(OWNER_ID))
async def admin_queue(client: Client, message: Message):
    cursor = queue_col.find({"status": {"$in": ["pending", "processing"]}})
    users = {}
    async for doc in cursor:
        users.setdefault(doc["user_id"], 0)
        users[doc["user_id"]] += 1
    text = "**Active Queues:**\n\n"
    for uid, count in list(users.items())[:15]:
        text += f"• User {uid}: {count} tasks\n"
    await message.reply(text or "No active queues")

@app.on_message(filters.command("clear") & filters.user(OWNER_ID))
async def admin_clear(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply("Usage: /clear <user_id>")
    try:
        uid = int(message.command[1])
        await clear_user_queue(uid)
        await message.reply(f"Cleared queue for user {uid}")
    except Exception:
        await message.reply("Invalid user ID")

# Media Group Handling (Albums)
@app.on_message(filters.media_group & filters.private)
async def handle_media_group(client: Client, message: Message):
    media_group_id = message.media_group_id
    if media_group_id not in MEDIA_GROUP_CACHE:
        MEDIA_GROUP_CACHE[media_group_id] = []
        # Schedule processing after timeout
        asyncio.create_task(process_media_group_after_delay(media_group_id))

    MEDIA_GROUP_CACHE[media_group_id].append(message)

async def process_media_group_after_delay(media_group_id: str):
    await asyncio.sleep(BATCH_TIMEOUT)
    messages = MEDIA_GROUP_CACHE.pop(media_group_id, [])
    if not messages:
        return

    chat_id = messages[0].chat.id
    user_id = messages[0].from_user.id
    batch_id = f"batch_{media_group_id}_{int(time.time())}"

    valid_count = 0
    for msg in messages:
        if not msg.document or not msg.document.file_name.lower().endswith(".mp3"):
            continue
        if msg.document.file_size > MAX_FILE_SIZE:
            await app.send_message(chat_id, f"❌ {msg.document.file_name} is too large (>1GB)")
            continue

        await add_to_queue(
            user_id=user_id,
            chat_id=chat_id,
            file_id=msg.document.file_id,
            file_name=msg.document.file_name,
            batch_id=batch_id
        )
        valid_count += 1

    if valid_count > 0:
        await app.send_message(chat_id, f"📥 Added {valid_count} MP3 files from album to queue.")
        if user_id not in USER_WORKERS:
            USER_WORKERS[user_id] = asyncio.create_task(user_worker(user_id))

# Single Document
@app.on_message(filters.document & filters.private & ~filters.media_group)
async def handle_single(client: Client, message: Message):
    doc = message.document
    if not doc.file_name.lower().endswith(".mp3"):
        return await message.reply("❌ Please send only .mp3 files.")
    if doc.file_size > MAX_FILE_SIZE:
        return await message.reply("❌ File too large (>1GB).")

    await add_to_queue(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        file_id=doc.file_id,
        file_name=doc.file_name
    )
    await message.reply("📥 Added to queue.")
    if message.from_user.id not in USER_WORKERS:
        USER_WORKERS[message.from_user.id] = asyncio.create_task(user_worker(message.from_user.id))

# ───────────────── STARTUP ─────────────────
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": OWNER_ID, "text": "𝐁𝐨𝐭 𝐑𝐞𝐬𝐭𝐚𝐫𝐭𝐞𝐝 𝐒𝐮𝐜𝐜𝐞𝐬𝐬𝐟𝐮𝐥𝐥𝐲 ✅"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.error(f"Notify failed: {e}")

async def resume_tasks():
    await queue_col.update_many({"status": "processing"}, {"$set": {"status": "pending"}})
    for f in DOWNLOAD_DIR.glob("*"):
        try:
            f.unlink()
        except Exception:
            pass

    users = await queue_col.distinct("user_id", {"status": "pending"})
    for uid in users:
        if uid not in USER_WORKERS:
            USER_WORKERS[uid] = asyncio.create_task(user_worker(uid))
    logger.info(f"Resumed tasks for {len(users)} users")

async def main():
    await app.start()
    await resume_tasks()
    notify_owner()
    logger.info("Bot started successfully")
    await asyncio.Event().wait()  # Keep running

if __name__ == "__main__":
    notify_owner()
    asyncio.get_event_loop().run_until_complete(main())
