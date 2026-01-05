import os
import asyncio
import time
import json
import logging
import traceback
import platform
import psutil
import requests

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from motor.motor_asyncio import AsyncIOMotorClient

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
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BOT_START_TIME = time.time()

# ================= BOT ====================
app = Client(
    "audio_queue_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50
)

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo.audio_bot
queue_col = db.queue

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
            f"📊 `{current//1024} / {total//1024 if total else 0} KB`\n"
            f"⚡ `{speed/1024:.2f} KB/s`\n"
            f"⏳ ETA: `{eta}s`\n"
            f"🧵 Queue: `{qinfo}`"
        )

        await msg.edit(text)

    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass  # never crash progress

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
        output_file
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, err = await proc.communicate()

    if proc.returncode != 0:
        logger.error(err.decode(errors="ignore"))
        raise RuntimeError("FFmpeg compression failed")

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

    out, _ = await proc.communicate()

    try:
        data = json.loads(out)
        return int(float(data["format"]["duration"]))
    except Exception:
        return 0

# ================= QUEUE ==================
async def add_queue(user_id, task):
    await queue_col.update_one(
        {"_id": user_id},
        {"$push": {"queue": task}, "$setOnInsert": {"active": False, "cancel": False}},
        upsert=True
    )

async def worker(user_id, chat_id):
    try:
        data = await queue_col.find_one({"_id": user_id})
        if not data or data.get("active"):
            return

        await queue_col.update_one({"_id": user_id}, {"$set": {"active": True}})

        while True:
            data = await queue_col.find_one({"_id": user_id})
            queue = data.get("queue", [])

            if not queue or data.get("cancel"):
                break

            task = queue[0]
            total = len(queue)

            msg = await app.send_message(chat_id, f"⬇️ Downloading `{task['file_name']}`")
            start = time.time()

            try:
                input_path = await app.download_media(
                    task["file_id"],
                    file_name=os.path.join(DOWNLOAD_DIR, task["file_name"]),
                    progress=progress,
                    progress_args=(msg, start, "Downloading", f"{total}/{total}")
                )

                output_path = input_path.replace(".", "_compressed.")

                await msg.edit("🎛 Compressing...")
                await compress_audio(input_path, output_path)

                duration = await get_audio_duration(output_path)

                await msg.edit("⬆️ Uploading...")
                await app.send_audio(
                    chat_id,
                    audio=output_path,
                    duration=duration,
                    title=os.path.splitext(task["file_name"])[0],
                    performer="Converted by Bot 🎵",
                    progress=progress,
                    progress_args=(msg, time.time(), "Uploading", f"{total}/{total}")
                )

                os.remove(input_path)
                os.remove(output_path)
                await msg.delete()

            except FloodWait as e:
                await asyncio.sleep(e.value)

            except Exception as e:
                logger.error(e)
                logger.error(traceback.format_exc())
                await msg.edit("❌ Failed to process file")

            await queue_col.update_one({"_id": user_id}, {"$pop": {"queue": -1}})

        await queue_col.update_one(
            {"_id": user_id},
            {"$set": {"active": False, "cancel": False}}
        )

    except Exception as e:
        logger.critical(traceback.format_exc())

# ================= COMMANDS =================
@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        "🎵 **Audio Queue Bot**\n\n"
        "📥 Send audio/document\n"
        "🧵 Per-user queue\n"
        "🎛 Auto compression\n"
        "❌ /cancel to stop"
    )

@app.on_message(filters.command("stats") & filters.private)
async def stats(_, m):
    msg = await m.reply("📊 Collecting stats...")
    ping = (time.time() - BOT_START_TIME) * 1000

    mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024

    await msg.edit(
        "📊 **Bot Stats**\n\n"
        f"⏱ Uptime: `{format_uptime(int(time.time()-BOT_START_TIME))}`\n"
        f"⚡ Ping: `{ping:.2f} ms`\n"
        f"🧠 RAM: `{mem:.2f} MB`\n"
        f"🐍 Python: `{platform.python_version()}`"
    )

@app.on_message(filters.command("cancel"))
async def cancel(_, m):
    await queue_col.update_one(
        {"_id": m.from_user.id},
        {"$set": {"cancel": True}}
    )
    await m.reply("❌ Queue cancelled")

@app.on_message(filters.audio | filters.document)
async def audio_handler(_, m: Message):
    file = m.audio or m.document

    task = {
        "file_id": file.file_id,
        "file_name": file.file_name or f"{file.file_unique_id}.mp3"
    }

    await add_queue(m.from_user.id, task)
    await m.reply("✅ Added to queue")
    asyncio.create_task(worker(m.from_user.id, m.chat.id))

# ================= START =================
def notify_owner():
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": OWNER_ID, "text": "✅ Bot Restarted Successfully"},
            timeout=10
        )
    except:
        pass

if __name__ == "__main__":
    notify_owner()
    app.run()
