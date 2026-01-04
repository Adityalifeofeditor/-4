import os
import asyncio
import subprocess

from pyrogram import Client, filters
from pyrogram.types import Message
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
import requests,json

API_ID = 27169529
API_HASH = "5d67602a4e0bbfabe669c0febeaf63b6"
BOT_TOKEN = "8574806355:AAGOXL5nDpzMvaEdhBAR_4vw3N2NXDABuJs"
OWNER = 6441347235

app = Client(
    "mp3_to_music",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    user = message.from_user

    await message.reply(
        "👋 **Welcome!**\n\n"
        "📥 Send me an **MP3 file as document**\n"
        "🎧 I will convert it into **Telegram Music** with duration\n"
        "🗜️ File size will be compressed automatically"
    )

def get_audio_duration(file_path: str) -> int:
    """Extract duration in seconds"""
    parser = createParser(file_path)
    if not parser:
        return 0
    metadata = extractMetadata(parser)
    if not metadata:
        return 0
    return int(metadata.get("duration").seconds)


async def compress_audio(input_file: str, output_file: str):
    """
    Compress audio using ffmpeg
    128k bitrate (you can change)
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-map_metadata", "-1",
        "-vn",
        "-ac", "2",
        "-b:a", "128k",
        output_file
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    await process.communicate()

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    user = message.from_user

    await message.reply(
        "👋 **Welcome!**\n\n"
        "📥 Send me an **MP3 file as document**\n"
        "🎧 I will convert it into **Telegram Music** with duration\n"
        "🗜️ File size will be compressed automatically"
    )

@app.on_message(filters.document & filters.private)
async def mp3_to_music(client: Client, message: Message):
    if not message.document.file_name.lower().endswith(".mp3"):
        return await message.reply("❌ Please send an **MP3 file** only.")

    msg = await message.reply("⏳ Processing your audio...")

    file_name = message.document.file_name
    input_path = os.path.join(DOWNLOAD_DIR, file_name)
    output_path = os.path.join(DOWNLOAD_DIR, f"compressed_{file_name}")

    # Download file
    await message.download(input_path)

    # Compress audio
    await compress_audio(input_path, output_path)

    # Get duration
    duration = get_audio_duration(output_path)

    # Send as Telegram Music
    await client.send_audio(
        chat_id=message.chat.id,
        audio=output_path,
        duration=duration,
        title=os.path.splitext(file_name)[0],
        performer="Converted by Bot 🎵"
    )

    await msg.delete()

    # Cleanup
    try:
        os.remove(input_path)
        os.remove(output_path)
    except:
        pass




def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER,
        "text": "𝐁𝐨𝐭 𝐑𝐞𝐬𝐭𝐚𝐫𝐭𝐞𝐝 𝐒𝐮𝐜𝐜𝐞𝐬𝐬𝐟𝐮𝐥𝐥𝐲 ✅"
    }
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Notify owner failed: {e}")

if __name__ == "__main__":
    notify_owner() 
    
app.run()
