import os
import asyncio
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from motor.motor_asyncio import AsyncIOMotorClient
import time
import sys
import platform
import pyrogram
import psutil
import logging

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MP3Bot")
# ================= CONFIG =================
API_ID = int(os.getenv("API_ID", 27169529))
API_HASH = os.getenv("API_HASH", "5d67602a4e0bbfabe669c0febeaf63b6")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8574806355:AAGOXL5nDpzMvaEdhBAR_4vw3N2NXDABuJs")
OWNER_ID = int(os.getenv("OWNER_ID", 6441347235))
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://adam822728:iP9ESt5vyfwDRxNB@cluster0.r82vfuz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= BOT ====================
app = Client(
    "audio_queue_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100
)

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo.audio_bot
queue_col = db.queue

# ================= UTILS ==================
def get_duration(file):
    parser = createParser(file)
    if not parser:
        return 0
    meta = extractMetadata(parser)
    if not meta:
        return 0
    return int(meta.get("duration").seconds)

def progress_bar(current, total):
    percent = current * 100 / total
    filled = int(percent / 10)
    return f"[{'█'*filled}{'░'*(10-filled)}] {percent:.1f}%"

async def progress(current, total, msg, start, stage, qinfo):
    now = time.time()
    speed = current / (now - start + 1)
    eta = (total - current) / speed if speed else 0

    text = (
        f"🎧 **{stage}**\n"
        f"{progress_bar(current, total)}\n"
        f"📊 `{current//1024} / {total//1024} KB`\n"
        f"⚡ `{speed/1024:.2f} KB/s`\n"
        f"⏳ ETA: `{int(eta)}s`\n"
        f"🧵 Queue: `{qinfo}`"
    )

    try:
        await msg.edit(text)
    except:
        pass

# ================= QUEUE ==================
async def add_queue(user_id, file):
    await queue_col.update_one(
        {"_id": user_id},
        {"$push": {"queue": file}, "$setOnInsert": {"active": False, "cancel": False}},
        upsert=True
    )

async def worker(user_id, chat_id):
    data = await queue_col.find_one({"_id": user_id})
    if not data or data["active"]:
        return

    await queue_col.update_one({"_id": user_id}, {"$set": {"active": True}})

    while True:
        data = await queue_col.find_one({"_id": user_id})
        if not data["queue"] or data.get("cancel"):
            break

        task = data["queue"][0]
        index = len(data["queue"])
        file_id = task["file_id"]
        name = task["file_name"]

        msg = await app.send_message(chat_id, f"⬇️ Downloading `{name}`")
        start = time.time()

        path = await app.download_media(
            file_id,
            file_name=f"{DOWNLOAD_DIR}/{name}",
            progress=progress,
            progress_args=(msg, start, "Downloading", f"{index}/{index}")
        )

        duration = get_duration(path)

        await msg.edit("⬆️ Uploading...")
        await app.send_audio(
            chat_id,
            audio=path,
            duration=duration,
            title=os.path.splitext(name)[0],
            progress=progress,
            progress_args=(msg, time.time(), "Uploading", f"{index}/{index}")
        )

        os.remove(path)
        await msg.delete()

        await queue_col.update_one(
            {"_id": user_id},
            {"$pop": {"queue": -1}}
        )

    await queue_col.update_one(
        {"_id": user_id},
        {"$set": {"active": False, "cancel": False}}
    )

# ================= COMMANDS =================
@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        "🎵 **Audio Queue Bot**\n\n"
        "📥 Send **any audio file**\n"
        "🧵 Per-user queue\n"
        "⏯ Resume supported\n"
        "❌ /cancel to stop batch"
    )

BOT_START_TIME = time.time()
def format_uptime(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)

    uptime = []
    if d: uptime.append(f"{d}d")
    if h: uptime.append(f"{h}h")
    if m: uptime.append(f"{m}m")
    uptime.append(f"{s}s")

    return " ".join(uptime)
@app.on_message(filters.command("stats") & filters.private)
async def stats_handler(client, message):
    start = time.time()

    msg = await message.reply("📊 Collecting stats...")
    end = time.time()

    ping = (end - start) * 1000
    uptime = int(time.time() - BOT_START_TIME)

    process = psutil.Process(os.getpid())
    memory = process.memory_info().rss / 1024 / 1024

    text = (
        "📊 **Bot Statistics**\n\n"
        f"⏱ **Uptime:** `{format_uptime(uptime)}`\n"
        f"⚡ **Ping:** `{ping:.2f} ms`\n"
        f"🧠 **RAM Usage:** `{memory:.2f} MB`\n\n"
        f"🐍 **Python:** `{platform.python_version()}`\n"
        f"🚀 **Pyrogram:** `{pyrogram.__version__}`"
    )

    await msg.edit(text)

@app.on_message(filters.command("cancel"))
async def cancel(_, m):
    await queue_col.update_one(
        {"_id": m.from_user.id},
        {"$set": {"cancel": True}}
    )
    await m.reply("❌ Queue cancelled")

@app.on_message(filters.audio | filters.document)
async def audio_handler(_, m: Message):
    if not (m.audio or m.document):
        return

    file = m.audio or m.document
    user_id = m.from_user.id

    task = {
        "file_id": file.file_id,
        "file_name": file.file_name or "audio"
    }

    await add_queue(user_id, task)
    await m.reply("✅ Added to queue")

    asyncio.create_task(worker(user_id, m.chat.id))
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": OWNER_ID, "text": "𝐁𝐨𝐭 𝐑𝐞𝐬𝐭𝐚𝐫𝐭𝐞𝐝 𝐒𝐮𝐜𝐜𝐞𝐬𝐬𝐟𝐮𝐥𝐥𝐲 ✅"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.error(f"Notify failed: {e}")

if __name__ == "__main__":
    notify_owner()
# ================= START ===================
app.run()
