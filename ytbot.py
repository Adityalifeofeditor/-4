from pyrogram import Client, filters
from yt_dlp import YoutubeDL
import os

API_ID = 123456        # your api_id
API_HASH = "your_api_hash"
BOT_TOKEN = "your_bot_token"

app = Client(
    "ytbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# YouTube download options
ydl_opts = {
    "outtmpl": "%(title)s.%(ext)s",
    "format": "mp4",
}


@app.on_message(filters.private & filters.text)
async def download_youtube(client, message):
    url = message.text.strip()

    # Very basic URL check
    if not ("youtu" in url):
        return await message.reply("Send a valid YouTube link!")

    await message.reply("⏳ Downloading...")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        await message.reply_video(video=file_path, caption=f"🎬 {info.get('title')}")
        os.remove(file_path)

    except Exception as e:
        await message.reply(f"❌ Error: `{e}`")


app.run()
