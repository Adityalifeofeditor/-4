import os
import asyncio
from collections import defaultdict
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pymongo import MongoClient
from google import genai

# =====================================================
# LOAD ENV
# =====================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID"))
MONGO_URI = os.getenv("MONGO_URI")
ENV_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN or not API_ID or not API_HASH or not OWNER_ID or not MONGO_URI:
    raise SystemExit("❌ Missing required environment variables.")

# =====================================================
# TELEGRAM BOT INIT
# =====================================================
app = Client(
    "ask_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

# =====================================================
# MONGODB (SYNC) - PYMONGO
# =====================================================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["ask_bot"]

users_col = db["users"]
settings_col = db["settings"]

# =====================================================
# SIMPLE FUNCTIONS FOR MONGODB
# =====================================================
def get_user(uid: int):
    """Return user or create one."""
    user = users_col.find_one({"_id": uid})
    if user:
        return user
    new_user = {"_id": uid, "points": 0, "stats": 0, "banned": False}
    users_col.insert_one(new_user)
    return new_user

def update_user(uid: int, data: dict):
    users_col.update_one({"_id": uid}, {"$set": data}, upsert=True)

def inc_user(uid: int, field: str, amount: int = 1):
    users_col.update_one({"_id": uid}, {"$inc": {field: amount}}, upsert=True)

def get_setting(key: str):
    doc = settings_col.find_one({"key": key})
    return doc["value"] if doc else None

def set_setting(key: str, value):
    settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def list_banned():
    return [u["_id"] for u in users_col.find({"banned": True})]

# =====================================================
# GEMINI
# =====================================================
gemini_client = None

def set_gemini_client(key: str):
    global gemini_client
    gemini_client = genai.Client(api_key=key)

def init_gemini():
    saved = get_setting("gemini_api_key")
    key = saved or ENV_GEMINI_API_KEY
    if key:
        set_gemini_client(key)

init_gemini()

async def query_gemini(prompt: str):
    """Safe wrapper to avoid blocking."""
    loop = asyncio.get_event_loop()

    def _call():
        res = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return getattr(res, "text", None) or str(res)

    return await loop.run_in_executor(None, _call)

def chunk_text(text, size=3500):
    for i in range(0, len(text), size):
        yield text[i:i + size]

# =====================================================
# KEYBOARD
# =====================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ask", callback_data="ask_btn")],
        [
            InlineKeyboardButton("Help", callback_data="help_btn"),
            InlineKeyboardButton("Stats", callback_data="stats_btn")
        ]
    ])

# =====================================================
# COMMAND: /start
# =====================================================
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "👋 *Welcome to Gemini Ask Bot!*\n\n"
        "Use /ask to begin.\n\n"
        "Reply to a message with /ask too!",
        reply_markup=main_keyboard()
    )

# =====================================================
# COMMAND: /help
# =====================================================
@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    await msg.reply(
        "*COMMANDS*\n"
        "• /ask <question>\n"
        "• Reply with /ask\n"
        "• /stats\n"
        "• /restart\n\n"
        "*Owner Commands*\n"
        "• /set_api <key>\n"
        "• /add_points <uid> <num>\n"
        "• /rem_points <uid> <num>\n"
        "• /ban <uid>\n"
        "• /unban <uid>\n"
        "• /banlist"
    )

# =====================================================
# COMMAND: /restart
# =====================================================
@app.on_message(filters.command("restart"))
async def restart(_, msg):
    await msg.reply("🔄 Restarted. Use /ask again.")

# =====================================================
# COMMAND: /stats
# =====================================================
@app.on_message(filters.command("stats"))
async def stats(_, msg):
    user = get_user(msg.from_user.id)
    total = get_setting("total_questions") or 0

    await msg.reply(
        f"*Your Stats:*\n"
        f"Questions: {user['stats']}\n"
        f"Points: {user['points']}\n\n"
        f"*Global:*\n{total} questions asked."
    )

# =====================================================
# ADMIN COMMANDS
# =====================================================
@app.on_message(filters.command("set_api"))
async def set_api(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    if len(msg.command) == 1:
        return await msg.reply("Send `/set_api YOUR_KEY`")

    # Accept inline: /set_api key...
    if len(msg.command) > 1:
        key = " ".join(msg.command[1:]).strip()

    elif message.reply_to_message:
        replied = message.reply_to_message

        if replied.text:
            key = replied.text
        elif replied.caption:
            key = replied.caption
    else:
        # or ask interactive
        asked = await app.ask(msg.chat.id, text="Send the Gemini API key (or /cancel):")
        key = (asked.text or "").strip()


    
    key = " ".join(msg.command[1:])
    set_setting("gemini_api_key", key)
    set_gemini_client(key)

    await msg.reply("✅ Gemini API key updated.")

@app.on_message(filters.command("add_points"))
async def add_points(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    if len(msg.command) < 3:
        return await msg.reply("Usage: /add_points <uid> <num>")

    uid = int(msg.command[1])
    amount = int(msg.command[2])

    inc_user(uid, "points", amount)
    await msg.reply(f"Added {amount} points to {uid}")

@app.on_message(filters.command("rem_points"))
async def rem_points(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    if len(msg.command) < 3:
        return await msg.reply("Usage: /rem_points <uid> <num>")

    uid = int(msg.command[1])
    amount = int(msg.command[2])

    inc_user(uid, "points", -abs(amount))
    await msg.reply(f"Removed {amount} points from {uid}")

@app.on_message(filters.command("ban"))
async def ban(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    if len(msg.command) < 2:
        return await msg.reply("Usage: /ban <uid>")

    uid = int(msg.command[1])
    update_user(uid, {"banned": True})
    await msg.reply(f"🚫 Banned {uid}")

@app.on_message(filters.command("unban"))
async def unban(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    if len(msg.command) < 2:
        return await msg.reply("Usage: /unban <uid>")

    uid = int(msg.command[1])
    update_user(uid, {"banned": False})
    await msg.reply(f"✅ Unbanned {uid}")

@app.on_message(filters.command("banlist"))
async def banlist(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Not allowed.")

    banned = list_banned()
    if not banned:
        return await msg.reply("No banned users.")

    await msg.reply("*Banned Users:*\n" + "\n".join(map(str, banned)))

# =====================================================
# COMMAND: /ask
# =====================================================
@app.on_message(filters.command("ask"))
async def ask_(app, msg):
    uid = msg.from_user.id
    user = get_user(uid)

    if user["banned"]:
        return await msg.reply("🚫 You are banned.")

    # --- Case 1: Reply
    if msg.reply_to_message and msg.reply_to_message.text:
        question = msg.reply_to_message.text

    # --- Case 2: Inline text
    elif len(msg.command) > 1:
        question = " ".join(msg.command[1:])

    # --- Case 3: Ask user
    else:
        ask_msg = await app.ask(uid, "✍️ Send your question:")
        question = ask_msg.text.strip()

    status = await msg.reply("⏳ Thinking...")

    # Gemini response
    try:
        answer = await query_gemini(question)
    except Exception as e:
        return await status.edit_text(f"⚠️ Error: {e}")

    # update stats
    inc_user(uid, "stats", 1)
    inc_user(uid, "points", 1)
    total = get_setting("total_questions") or 0
    set_setting("total_questions", total + 1)

    await status.edit_text(f"*Question:*\n{question}\n\n*Answer:*")

    for chunk in chunk_text(answer):
        await msg.reply(chunk)

# =====================================================
# CALLBACK BUTTONS
# =====================================================
@app.on_callback_query()
async def callback(_, cq):
    uid = cq.from_user.id
    data = cq.data

    if data == "ask_btn":
        await cq.message.reply("✍️ Send your question using /ask")
        await cq.answer()

    elif data == "help_btn":
        await cq.message.reply("Use /help to see full commands.")
        await cq.answer()

    elif data == "stats_btn":
        user = get_user(uid)
        await cq.message.reply(f"You asked {user['stats']} questions.")
        await cq.answer()

# =====================================================
# COMMAND: /see
# =====================================================
@app.on_message(filters.command("see"))
async def see_cmd(app, message):
    if len(message.command) > 1:
        text = " ".join(message.command[1:])
        return await message.reply(f"🟦 **You typed:**\n`{text}`")

    if message.reply_to_message:
        rep = message.reply_to_message
        if rep.text:
            return await message.reply(f"🟧 **Replied Text:**\n`{rep.text}`")
        if rep.caption:
            return await message.reply(f"🟨 **Replied Caption:**\n`{rep.caption}`")
        return await message.reply("⚠️ No text/caption found.")

    ask = await app.ask(message.chat.id, "👀 Send text:")
    await message.reply(f"🟩 **You typed:**\n`{ask.text}`")

# =====================================================
# BOT RUN
# =====================================================
if __name__ == "__main__":
    print("🚀 Bot Started!")
    app.run()
