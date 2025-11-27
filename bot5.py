
import traceback
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from google import genai
import os, re, sys, json, asyncio, requests
import time
from datetime import datetime, timedelta
load_dotenv()

BOT_STARTED_AT = time.time()


# -------------------------------
# Required env vars
# -------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
OWNER_ID = os.getenv("OWNER_ID")
MONGO_URI = os.getenv("MONGO_URI")

# Optional/configurable envs
ENV_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")   # channel for tracebacks (e.g. -1001234567890)
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")       # group for Q&A logs (e.g. -1009876543210)
BONUS_POINTS = int(os.getenv("BONUS_POINTS", "20"))
CREATE_INDEXES = os.getenv("CREATE_INDEXES", "yes").lower() in ("1", "yes", "true", "y")

# ---- basic validation ----
if not BOT_TOKEN or not API_ID or not API_HASH or not OWNER_ID or not MONGO_URI:
    raise SystemExit("Missing one of required env vars: BOT_TOKEN, API_ID, API_HASH, OWNER_ID, MONGO_URI")

API_ID = int(API_ID)
OWNER_ID = int(OWNER_ID)

# -------------------------------
# Initialize Telegram client
# -------------------------------
app = Client(
    "ask_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH,
)

# -------------------------------
# MongoDB (PyMongo sync)
# -------------------------------
mongo = MongoClient(MONGO_URI)
db = mongo.get_database("ask_bot")   # default DB name; change if you like
users_col = db["users"]
settings_col = db["settings"]

# create helpful indexes if requested
if CREATE_INDEXES:
    users_col.create_index("points")
    users_col.create_index("stats")
    users_col.create_index("last_bonus")
    settings_col.create_index("key", unique=True)

# -------------------------------
# Gemini client holder
# -------------------------------
gemini_client = None
def set_gemini_client(key: str):
    global gemini_client
    gemini_client = genai.Client(api_key=key)

# load key from DB or env
saved_key_doc = settings_col.find_one({"key": "gemini_api_key"})
if saved_key_doc and saved_key_doc.get("value"):
    try:
        set_gemini_client(saved_key_doc["value"])
    except Exception:
        gemini_client = None
elif ENV_GEMINI_API_KEY:
    try:
        set_gemini_client(ENV_GEMINI_API_KEY)
    except Exception:
        gemini_client = None

# -------------------------------
# Simple helpers: DB access
# -------------------------------
def get_setting(key: str):
    doc = settings_col.find_one({"key": key})
    return doc["value"] if doc else None

def set_setting(key: str, value):
    settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def get_user(uid: int):
    doc = users_col.find_one({"_id": uid})
    if doc:
        return doc
    new = {"_id": uid, "points": 0, "stats": 0, "banned": False, "last_bonus": None}
    users_col.insert_one(new)
    return new

def inc_user_field(uid: int, field: str, amount: int = 1):
    users_col.update_one({"_id": uid}, {"$inc": {field: amount}}, upsert=True)

def set_user_field(uid: int, field: str, value):
    users_col.update_one({"_id": uid}, {"$set": {field: value}}, upsert=True)

def list_banned(limit=500):
    return [d["_id"] for d in users_col.find({"banned": True}).limit(limit)]

# -------------------------------
# Logging helpers
# -------------------------------
async def send_error_traceback(exc: Exception, context: str = ""):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = f"⚠️ *Error in bot*\nContext: `{context}`\n\n```\n{tb[:3900]}\n```"
    # send to log channel if set
    if LOG_CHANNEL_ID:
        try:
            await app.send_message(int(LOG_CHANNEL_ID), text)
        except Exception as e:
            print("Failed to send traceback to log channel:", e)
            print(tb)
    else:
        print("Traceback (no LOG_CHANNEL_ID):")
        print(tb)

async def log_qa(user, question: str, answer: str):
    text = (
        f"💬 *Q&A Log*\n\n"
        f"User: `{user.get('_id')}` ({user.get('username') or 'no-username'})\n"
        f"Name: {user.get('name') or '—'}\n\n"
        f"*Question:*\n{question}\n\n*Answer:*\n{(answer or '')[:4000]}"
    )
    if LOG_GROUP_ID:
        try:
            await app.send_message(int(LOG_GROUP_ID), text)
        except Exception as e:
            print("Failed to send Q&A log:", e)
            # fallback printing
            print(text)
    else:
        print("Q&A log (no LOG_GROUP_ID):")
        print(text)

# -------------------------------
# Utility: run Gemini safely in executor (non-blocking)
# -------------------------------
def chunk_text(text, size=3500):
    for i in range(0, len(text or ""), size):
        yield text[i:i+size]

async def query_gemini(prompt: str):
    if gemini_client is None:
        raise RuntimeError("Gemini API key not set. Use admin /admin_settings -> API Key -> Set API Key or /set_api.")
    loop = asyncio.get_event_loop()
    def _call():
        # synchronous network call performed in executor
        res = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return getattr(res, "text", None) or str(res)
    return await loop.run_in_executor(None, _call)

# -------------------------------
# UI Keyboards
# -------------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Ask a question", callback_data="ask_btn")],
        [
            InlineKeyboardButton("🛠️ Admin", callback_data="admin_settings"),
            InlineKeyboardButton("📊 Stats", callback_data="stats_btn"),
        ],
        [InlineKeyboardButton("⭐ Bonus (daily)", callback_data="bonus_btn")],
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Points", callback_data="admin_add_points"),
         InlineKeyboardButton("➖ Remove Points", callback_data="admin_rem_points")],
        [InlineKeyboardButton("🚫 Ban", callback_data="admin_ban"),
         InlineKeyboardButton("✅ Unban", callback_data="admin_unban")],
        [InlineKeyboardButton("🔑 API Key", callback_data="admin_api_menu")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")],
    ])

def api_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 Set API Key", callback_data="api_set")],
        [InlineKeyboardButton("👁️ View API Key", callback_data="api_view")],
        [InlineKeyboardButton("🗑️ Remove API Key", callback_data="api_remove")],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_settings")],
    ])

# -------------------------------
# Commands
# -------------------------------




@app.on_message(filters.command("start"))
async def start(_, msg):
    try:
        user = get_user(msg.from_user.id)
        # store some quick meta for logging
        if msg.from_user.username:
            set_user_field(msg.from_user.id, "username", msg.from_user.username)
        if msg.from_user.first_name:
            set_user_field(msg.from_user.id, "name", msg.from_user.first_name)
        await msg.reply(
            "👋 *Welcome to Gemini Ask Bot*\n\n"
            "You can ask questions in 3 ways:\n"
            "• `/ask your question`\n"
            "• Reply to any message with `/ask`\n"
            "• Send `/ask` alone and I will wait for your question\n\n"
            "Use /help for full info.",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        await send_error_traceback(e, "start handler")

@app.on_message(filters.command("admin_settings") & filters.user(OWNER_ID))
async def admin_start(_, msg):
    try:
        await msg.reply(
            "👋 *Welcome to Gemini Ask Bot*\n\n"
            "Here is your admin panel",
            reply_markup=admin_keyboard()
        )
    except Exception as e:
        await send_error_traceback(e, "admin_start")
        
@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    try:
        await msg.reply(
            "📚 *Help / Commands*\n\n"
            "• `/ask <question>` — Ask instantly\n"
            "• Reply to a message with `/ask`\n"
            "• `/ask` alone — I'll wait for your next message\n"
            "• `/restart` — Clear pending request\n"
            "• `/stats` — Your stats\n\n"
            "Owner-only (admin) commands via UI: press *Admin* or use `/admin_settings`.\n"
            "• `/setcommands` — register bot commands\n",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        await send_error_traceback(e, "help handler")

@app.on_message(filters.command("setcommands"))
async def setcommands(_, msg):
    try:
        # register commands (friendly list)
        cmds = [
            ("start", "Start / welcome"),
            ("ask", "Ask a question"),
            ("stats", "Your usage statistics"),
            ("bonus", "Daily bonus points"),
            ("balance", "how many points do i have"),
            ("help", "Show help"),
        ]
        if msg.from_user.id == OWNER_ID:
            cmds += [
                ("admin_settings", "Open admin menu"),
                ("set_api", "Set Gemini API key (owner)"),
            ]
        await app.set_bot_commands([{"command": c, "description": d} for c, d in cmds])
        await msg.reply("✅ Bot commands updated.")
    except Exception as e:
        await send_error_traceback(e, "setcommands")

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    try:
        await msg.reply("🔄 Session restarted. Use /ask to ask a question again.")
    except Exception as e:
        await send_error_traceback(e, "restart")


# Admin helper to parse target & amount (works for reply or inline)
def _parse_target_and_amount_from_text(text, reply_msg):
    """
    text: the free text from user (owner) e.g. "123456 10" or "10" (if replying)
    reply_msg: pyrogram.Message or None (if user replied to someone's message)
    returns (target_id, amount) or (None, None)
    """
    try:
        parts = text.strip().split()
        if reply_msg and len(parts) == 1:
            target = reply_msg.from_user.id
            amount = int(parts[0])
            return target, amount
        elif len(parts) >= 2:
            target = int(parts[0])
            amount = int(parts[1])
            return target, amount
    except Exception:
        return None, None
    return None, None

# Admin: /set_api (shortcut)
@app.on_message(filters.command("set_api"))
async def set_api_cmd(_, msg):
    try:
        if msg.from_user.id != OWNER_ID:
            return await msg.reply("❌ Only the owner can use this command.")
        if len(msg.command) > 1:
            key = " ".join(msg.command[1:]).strip()
        else:
            asked = await app.ask(msg.chat.id, text="Send the Gemini API key (or /cancel):")
            key = (asked.text or "").strip()
        if not key:
            return await msg.reply("No key provided. Cancelled.")
        set_setting("gemini_api_key", key)
        set_gemini_client(key)
        await msg.reply("✅ Gemini API key saved and applied.")
    except Exception as e:
        await send_error_traceback(e, "set_api_cmd")

# -------------------------------


THINKING_FRAMES = [
    "⏳ Thinking…",
    "⌛ Thinking 🤔",
    "⏳ Thinking 🤔🤔",
    "⌛ Thinking 🤔🤔🤔",
    "⏳ Thinking 🧐",
    "⌛ Thinking 🧐🧐",
    "⏳ Thinking 🧐🧐🧐",
]

async def animate_status(message):
    """
    Continuously edits the message text with animation frames.
    Returns when cancelled.
    """
    try:
        i = 0
        while True:
            frame = THINKING_FRAMES[i % len(THINKING_FRAMES)]
            await message.edit_text(frame)
            i += 1
            await asyncio.sleep(0.7)   # speed of animation
    except Exception as e:
        await send_error_traceback(e, "query_gemini in ask_handler")

# Main Ask handler
# -------------------------------
@app.on_message(filters.command("ask"))
async def ask_handler(app_obj, msg):
    try:
        uid = msg.from_user.id
        user = get_user(uid)
        if user.get("banned"):
            return await msg.reply("🚫 You are banned from using this bot.")
            
        # 1) inline: /ask question
        if len(msg.command) > 1:
            question = " ".join(msg.command[1:])
            
        # 2) replied message
        elif msg.reply_to_message:
            replied = msg.reply_to_message
            if getattr(msg, "text", None):
                question = replied.text
            elif getattr(replied, "caption", None):
                question = replied.caption
            else:
                return await msg.reply_text("⚠️ Replied message has no text/caption.")

        # 3) interactive ask
        else:
            asked = await app.ask(uid, text="✍️ Send your question (or /cancel):")
            question = (asked.text or "").strip()
            if not question:
                return await msg.reply("❌ No question provided. Cancelled.")
                
        status = await msg.reply("⏳ Thinking...")
        
        try:
            animation_task = asyncio.create_task(animate_status(status))
        except Exception as e:
            await send_error_traceback(e, "query_gemini in ask_handler")
            
        try:
            # Stop animation

            try:
                animation_task.cancel()
            except Exception as e:
                await send_error_traceback(e, "query_gemini in ask_handler")
                    
            answer = await query_gemini(question)
        except Exception as e:
            await status.edit_text(f"⚠️ Error while querying Gemini: {e}")
            await send_error_traceback(e, "query_gemini in ask_handler")
            return
        # increment stats in DB
        inc_user_field(uid, "stats", 1)
        inc_user_field(uid, "points", 1)
        total = get_setting("total_questions") or 0
        set_setting("total_questions", (total or 0) + 1)
        # deliver answer (chunked)
        await status.edit_text(f"*Question:*\n{question}\n\n*Answer:*")
        for chunk in chunk_text(answer):
            await app.send_message(msg.chat.id, chunk)
        # log Q&A to group if configured
        try:
            # update some meta
            set_user_field(uid, "username", msg.from_user.username or "")
            set_user_field(uid, "name", msg.from_user.first_name or "")
            await log_qa(get_user(uid), question, answer)
        except Exception as e:
            # do not interrupt user flow; just log locally & to error channel
            print("Failed to log Q&A:", e)
            await send_error_traceback(e, "log_qa")
    except Exception as e:
        await send_error_traceback(e, "ask_handler top-level")

# -------------------------------
# /see command (user-friendly)
# -------------------------------
@app.on_message(filters.command("see"))
async def see_cmd(app_obj, message):
    try:
        if len(message.command) > 1:
            user_text = " ".join(message.command[1:])
            return await message.reply_text(f"🟦 **You typed:**\n`{user_text}`")
        if message.reply_to_message:
            replied = message.reply_to_message
            if getattr(replied, "text", None):
                return await message.reply_text(f"🟧 **Replied text:**\n`{replied.text}`")
            elif getattr(replied, "caption", None):
                return await message.reply_text(f"🟨 **Replied caption:**\n`{replied.caption}`")
            else:
                return await message.reply_text("⚠️ Replied message has no text/caption.")
        ask_msg = await app.ask(chat_id=message.chat.id, text="👀 **Send me something to display**")
        await ask_msg.reply_text(f"🟩 **You typed:**\n`{ask_msg.text}`")
    except Exception as e:
        await send_error_traceback(e, "see_cmd")

# -------------------------------
# /bonus (daily)
# -------------------------------
@app.on_message(filters.command("bonus"))
async def bonus_cmd(_, msg):
    try:
        uid = msg.from_user.id
        user = get_user(uid)
        last = user.get("last_bonus")
        now = datetime.utcnow()
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
            except Exception:
                last_dt = None
        else:
            last_dt = None
        if last_dt and (now - last_dt) < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last_dt)
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            return await msg.reply(f"⏳ You've already taken the daily bonus. Come back in {hours}h {minutes}m.")
        # grant bonus
        points = BONUS_POINTS
        inc_user_field(uid, "points", points)
        set_user_field(uid, "last_bonus", now.isoformat())
        await msg.reply(f"🎉 You received *{points}* points! (Daily bonus)")
    except Exception as e:
        await send_error_traceback(e, "bonus_cmd")
        
@app.on_message(filters.command("balance"))
async def balance_cmd(_, message):
    uid = message.from_user.id
    
    # Fetch user document
    user = await db.users.find_one({"_id": uid})
    
    # If user doesn't exist, create default entry
    if not user:
        user = {"_id": uid, "points": 0, "stats": 0, "banned": False}
        await db.users.insert_one(user)

    points = user.get("points", 0)

    await message.reply_text(
        f"💰 **Your Balance:**\n\n"
        f"⭐ **{points} Points**"
    )

@app.on_message(filters.command("stats"))
async def stats_cmd(app, message):
    try:

        start_ping = time.time()
    
        # --- Calculate API latency (ping)
        pong_msg = await message.reply("⏱️ Calculating ping...")
        ping_ms = (time.time() - start_ping) * 1000
        await pong_msg.delete()
    
        # --- Bot Uptime
        now = time.time()
        uptime_sec = int(now - BOT_STARTED_AT)
        uptime_str = str(timedelta(seconds=uptime_sec))
    
        # --- Fetch user stats
        uid = message.from_user.id
        user = users_col.find_one({"_id": uid}) or {"points": 0, "stats": 0}
    
        # --- Global stats
        total_users = users_col.count_documents({})
        total_questions = get_setting("total_questions") or 0
    
        await message.reply(
            f"📊 **Bot Statistics**\n\n"
            f"👤 **Your Stats**\n"
            f"• Questions Asked: `{user.get('stats', 0)}`\n"
            f"• Points: `{user.get('points', 0)}`\n\n"
            f"🌍 **Global Stats**\n"
            f"• Total Users: `{total_users}`\n"
            f"• Total Questions Asked: `{total_questions}`\n\n"
            f"🖥️ **System**\n"
            f"• Uptime: `{uptime_str}`\n"
            f"• Ping: `{ping_ms:.2f} ms`"
        )
    except Exception as e:
        await send_error_traceback(e, "stats")

# -------------------------------
# Callback query: Admin menu + API submenu
# -------------------------------
@app.on_callback_query()
async def callback_query_handler(_, cq):
    try:
        uid = cq.from_user.id
        data = cq.data or ""
        # Open admin settings (owner only)
        if data == "admin_settings":
            if uid != OWNER_ID:
                await cq.answer("🔒 Owner only", show_alert=True)
                return
            await cq.message.edit_text("⚙️ *Admin Settings*", reply_markup=admin_keyboard())
            await cq.answer()
            return
        # Return to main admin + back
        if data == "admin_back":
            await cq.message.edit_text("⚙️ *Admin Settings*", reply_markup=admin_keyboard())
            await cq.answer()
            return
        # Add points
        if data == "admin_add_points":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            asked = await app.ask(uid, "Send: `<user_id> <amount>` OR reply to a user's message with `<amount>`")
            target, amount = _parse_target_and_amount_from_text(asked.text or "", asked.reply_to_message)
            if target is None:
                return await app.send_message(uid, "❌ Invalid input. Use: `<user_id> <amount>` or reply + `<amount>`")
            inc_user_field(target, "points", amount)
            await app.send_message(uid, f"✅ Added {amount} points to `{target}`")
            return await cq.answer()
        # Remove points
        if data == "admin_rem_points":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            asked = await app.ask(uid, "Send: `<user_id> <amount>` OR reply to a user's message with `<amount>`")
            target, amount = _parse_target_and_amount_from_text(asked.text or "", asked.reply_to_message)
            if target is None:
                return await app.send_message(uid, "❌ Invalid input.")
            inc_user_field(target, "points", -abs(amount))
            await app.send_message(uid, f"✅ Removed {amount} points from `{target}`")
            return await cq.answer()
        # Ban user
        if data == "admin_ban":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            asked = await app.ask(uid, "Send: `<user_id>` OR reply to a user's message to ban them.")
            text = (asked.text or "").strip()
            target_id = None
            if asked.reply_to_message:
                target_id = asked.reply_to_message.from_user.id
            else:
                try:
                    target_id = int(text.split()[0])
                except Exception:
                    target_id = None
            if not target_id:
                return await app.send_message(uid, "❌ Invalid user id.")
            set_user_field(target_id, "banned", True)
            await app.send_message(uid, f"🚫 User `{target_id}` has been banned.")
            return await cq.answer()
        # Unban user
        if data == "admin_unban":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            asked = await app.ask(uid, "Send: `<user_id>` OR reply to a user's message to unban them.")
            text = (asked.text or "").strip()
            target_id = None
            if asked.reply_to_message:
                target_id = asked.reply_to_message.from_user.id
            else:
                try:
                    target_id = int(text.split()[0])
                except Exception:
                    target_id = None
            if not target_id:
                return await app.send_message(uid, "❌ Invalid user id.")
            set_user_field(target_id, "banned", False)
            await app.send_message(uid, f"✅ User `{target_id}` has been unbanned.")
            return await cq.answer()
        # API menu
        if data == "admin_api_menu" or data == "api_menu":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            await cq.message.edit_text("🔑 *API Key Menu*", reply_markup=api_menu_keyboard())
            return await cq.answer()
        # API set
        if data == "api_set":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            asked = await app.ask(uid, "Send the Gemini API key (or /cancel):")
            key = (asked.text or "").strip()
            if not key:
                return await app.send_message(uid, "Cancelled — no key provided.")
            set_setting("gemini_api_key", key)
            set_gemini_client(key)
            await app.send_message(uid, "✅ Gemini API key saved and applied.")
            return await cq.answer()
        # API view
        if data == "api_view":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            key = get_setting("gemini_api_key") or ENV_GEMINI_API_KEY
            if not key:
                return await app.send_message(uid, "No Gemini API key set.")
            show = ("*" + key[-4:]).rjust(len(key), "•") if len(key) > 4 else ("*" + key)
            await app.send_message(uid, f"🔐 Gemini API Key: `{show}`")
            return await cq.answer()
        # API remove
        if data == "api_remove":
            if uid != OWNER_ID:
                return await cq.answer("🔒 Owner only", show_alert=True)
            set_setting("gemini_api_key", "")
            # also clear runtime
            try:
                set_gemini_client("")
            except Exception:
                pass
            await app.send_message(uid, "🗑️ Gemini API key removed.")
            return await cq.answer()
        # Ask button from main keyboard
        if data == "ask_btn":
            await cq.answer()
            await cq.message.reply("✍️ Send `/ask` to ask a question (or reply to a message with `/ask`).")
            return
        # Stats button
        if data == "stats_btn":
            u = get_user(uid)
            await cq.answer()
            await cq.message.reply(f"You asked {u.get('stats',0)} questions total.")
            return
        # Bonus button
        if data == "bonus_btn":
            await cq.answer()
            return await bonus_cmd(None, cq.message)  # reuse bonus logic
        # fallback
        await cq.answer()
    except Exception as e:
        await send_error_traceback(e, "callback_query_handler")


# -------------------------------
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_ID,
        "text": "𝐁𝐨𝐭 𝐑𝐞𝐬𝐭𝐚𝐫𝐭𝐞𝐝 𝐒𝐮𝐜𝐜𝐞𝐬𝐬𝐟𝐮𝐥𝐥𝐲 ✅"
    }
    requests.post(url, data=data)
# -------------------------------

def reset_and_set_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"

    # General users ke liye commands
    general_commands = [
        {"command": "start", "description": "✅ Check Alive the Bot"},
        {"command": "cancel", "description": "🚫 Stop the ongoing process"},
        {"command": "id", "description": "🆔 Get Your ID"},
        {"command": "ask", "description": "👁️ View Bot Activity"},
        {"command": "bonus", "description": "👁️ get free points"},
        {"command": "balance", "description": "👁️ View your points"},
    ]
    # Owner ke liye extra commands
    owner_commands = general_commands + [
        {"command": "broadcast", "description": "📢 Broadcast to All Users"},
        {"command": "broadusers", "description": "👨‍❤️‍👨 All Broadcasting Users"},
        {"command": "add_user", "description": "▶️ Add Authorisation"},
        {"command": "rem_user", "description": "⏸️ Remove Authorisation "},
        {"command": "set_api", "description": "👨‍👨‍👧‍👦 ai api key"},
        {"command": "admin_settings", "description": "👁️ Admin settings"},
        {"command": "restart", "description": "✅ Reset the Bot"}
    ]

    # General users ke liye set commands (scope default)
    requests.post(url, json={
        "commands": general_commands,
        "scope": {"type": "default"},
        "language_code": "en"
    })

    # Owner ke liye set commands (scope user)
    requests.post(url, json={
        "commands": owner_commands,
        "scope": {"type": "chat", "chat_id": OWNER_ID},  # OWNER variable me chat id hona chahiye
        "language_code": "en"
    })

# -------------------------------
# Run bot
# -------------------------------
if __name__ == "__main__":
    try:
        notify_owner() 
    except Exception as e:
        print("Failed notify_owner handler:", e)
        
    try:
        reset_and_set_commands() 
    except Exception as e:
        print("Failed to reset_and_set_commands handler:", e)

    print("🚀 Running bot...")
    app.run()
