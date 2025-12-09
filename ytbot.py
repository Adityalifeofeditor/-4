import os
import requests
from datetime import datetime
from pyrogram import Client, filters
from yt_dlp import YoutubeDL

# ==========================
# ENVIRONMENT VARIABLES
# ==========================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")        # Owner's Telegram ID (string)
# ==========================


# ---------------------------------------
# Pyrogram Bot Client
# ---------------------------------------
app = Client(
    "yt_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ---------------------------------------
# Notify owner on bot restart
# ---------------------------------------
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_ID,
        "text": (
            "🚀 **Bot Restarted Successfully!** ✅\n\n"
            f"📊 **Status:** Online\n"
            f"⏰ **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        "parse_mode": "Markdown"
    }
    requests.post(url, data=data)


# ---------------------------------------
# /start command
# ---------------------------------------
@app.on_message(filters.command("start"))
async def start_cmd(_, message):
    await message.reply(
        "👋 **Hello!**\n"
        "Send me any **YouTube link**, and I will download the video for you! 📥🎬"
    )


# ---------------------------------------
# YouTube downloader
# ---------------------------------------
@app.on_message(filters.private & filters.text)
async def download_vid(_, message):

    url = message.text.strip()

    if not ("youtu" in url):
        return await message.reply("❌ Please send a valid YouTube link.")

    await message.reply("⏳ Downloading... Please wait.")

    ydl_opts = {
        "format": "mp4",
        "outtmpl": "%(title)s.%(ext)s"
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        await message.reply_video(
            video=file_path,
            caption=f"🎬 **{info.get('title')}**\nUploaded successfully!"
        )

        os.remove(file_path)

    except Exception as e:
        await message.reply(f"❌ Error:\n`{e}`")


# ---------------------------------------
# Start Bot
# ---------------------------------------
if __name__ == "__main__":

    try:
        notify_owner()
        print("✅ Owner notified successfully!")
    except Exception as e:
        print("⚠️ Failed notify_owner handler:", e)

    print("🚀 Starting YouTube Downloader Bot…")
    app.run()
    print("🌐 Bot is now online! ✅")
    
