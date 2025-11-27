
import traceback
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pymongo import MongoClient
from google import genai
import os, re, sys, json, asyncio, requests
import time
from datetime import datetime, timedelta
from pytz import timezone

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
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID") # channel for tracebacks (e.g. -1001234567890)
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID") # group for Q&A logs (e.g. -1009876543210)
WELCOME_POINT = int(os.getenv("WELCOME_POINT", "50"))
BONUS_POINTS = int(os.getenv("BONUS_POINTS", "20"))
ASK_MIN_POINTS = int(os.getenv("ASK_MIN_POINTS ", "1")) # set your minimum required points here
CREATE_INDEXES = os.getenv("CREATE_INDEXES", "yes").lower() in ("1", "yes", "true", "y")

# ---- basic validation ----
if not BOT_TOKEN or not API_ID or not API_HASH or not OWNER_ID or not MONGO_URI:
    raise SystemExit("❌ Missing one of required env vars: BOT_TOKEN, API_ID, API_HASH, OWNER_ID, MONGO_URI")

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
db = mongo.get_database("ai_ask_bot") # default DB name; change if you like
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

def set_model(user_id, model):
    db.settings.update_one(
        {"_id": user_id},
        {"$set": {"ai_model": model}},
        upsert=True
    )

def get_model(user_id):
    data = db.settings.find_one({"_id": user_id}) or {}
    return data.get("ai_model", "gemini-2.5-flash")
    
#def get_model(user_id):
    #data = db.settings.find_one({"_id": user_id})
    #return data.get("ai_model", "gemini-2.5-flash")  # default

# -------------------------------
# Logging helpers
# -------------------------------
async def send_error_traceback(exc: Exception, context: str = ""):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = f"⚠️ **Error in bot**\n**Context:** `{context}`\n\n```\n{tb[:3900]}\n```"
    # send to log channel if set
    if LOG_CHANNEL_ID:
        try:
            await app.send_message(int(LOG_CHANNEL_ID), text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print("Failed to send traceback to log channel:", e)
            print(tb)
    else:
        print("Traceback (no LOG_CHANNEL_ID):")
        print(tb)

async def log_qa(user, question: str, answer: str):
    text = (
        f"💬 **Q&A Log**\n\n"
        f"👤 **User:** `{user.get('_id')}` ({user.get('username') or 'no-username'})\n"
        f"📛 **Name:** {user.get('name') or '—'}\n\n"
        f"❓ **Question:**\n{question}\n\n"
        f"💡 **Answer:**\n{(answer or '')[:4000]}"
    )
    if LOG_GROUP_ID:
        try:
            await app.send_message(int(LOG_GROUP_ID), text, parse_mode=ParseMode.MARKDOWN)
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

user_model = get_model(OWNER_ID)
async def query_gemini(prompt: str):
    if gemini_client is None:
        raise RuntimeError("❌ Gemini API key not set. Use admin /admin_settings → API Key → Set API Key or /set_api.")
    loop = asyncio.get_event_loop()
    def _call():
        # synchronous network call performed in executor
        res = gemini_client.models.generate_content(model=user_model, contents=prompt)
        return getattr(res, "text", None) or str(res)
    return await loop.run_in_executor(None, _call)

# -------------------------------
# UI Keyboards
# -------------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ ask a question", callback_data="ask_btn")],
        [
            InlineKeyboardButton("🛠️ admin panel", callback_data="admin_settings"),
            InlineKeyboardButton("📊 statistics", callback_data="stats_btn"),
        ],
        [
            InlineKeyboardButton("⭐ daily bonus", callback_data="bonus_btn"),
            InlineKeyboardButton("⚖️ my balance", callback_data="balance_btn"),
        ],
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ add points", callback_data="admin_add_points"),
         InlineKeyboardButton("➖ remove points", callback_data="admin_rem_points")],
        [InlineKeyboardButton("🚫 ban user", callback_data="admin_ban"),
         InlineKeyboardButton("✅ unban user", callback_data="admin_unban")],
        [InlineKeyboardButton("🔑 api key", callback_data="admin_api_menu"),
         InlineKeyboardButton("🤖 ai model", callback_data="ai_model_menu")],
        [InlineKeyboardButton("◀️ back to main", callback_data="admin_back")],
    ])

def api_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 set api key", callback_data="api_set")],
        [InlineKeyboardButton("👁️ view api key", callback_data="api_view")],
        [InlineKeyboardButton("🗑️ remove api key", callback_data="api_remove")],
        [InlineKeyboardButton("◀️ back to admin", callback_data="admin_settings")],
    ])

def ai_model_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌩 gemini-2.5-pro", callback_data="model_g25pro")
        ],
        [
            InlineKeyboardButton("⚡ gemini-2.5-flash", callback_data="model_g25flash"),
        ],
        [
            InlineKeyboardButton("🧪 gemini-2.5-flash-preview", callback_data="model_g25flashprev"),
        ],
        [
            InlineKeyboardButton("💡 gemini-2.5-flash-lite", callback_data="model_g25lite"),
        ],
        [
            InlineKeyboardButton("🔬 gemini-2.5-flash-lite-preview", callback_data="model_g25liteprev"),
        ],
        [
            InlineKeyboardButton("⚡ gemini-2.0-flash", callback_data="model_g20flash"),
        ],
        [
            InlineKeyboardButton("🌙 gemini-2.0-flash-lite", callback_data="model_g20lite"),
        ],
        [InlineKeyboardButton("◀️ back to admin", callback_data="admin_settings")],
    ])

@app.on_callback_query(filters.regex("^ai_model_menu$"))
async def open_ai_model_menu(_, cq):
    await cq.answer()
    await cq.message.edit_text(
        "🤖 **Choose AI Model**\n\nSelect the model you want the bot to use:",
        reply_markup=ai_model_keyboard()
    )

@app.on_callback_query(filters.regex("^model_"))
async def set_ai_model(_, cq):
    await cq.answer()

    model_map = {
        "model_g25pro": "gemini-2.5-pro",
        "model_g25flash": "gemini-2.5-flash",
        "model_g25flashprev": "gemini-2.5-flash-preview",
        "model_g25lite": "gemini-2.5-flash-lite",
        "model_g25liteprev": "gemini-2.5-flash-lite-preview",
        "model_g20flash": "gemini-2.0-flash",
        "model_g20lite": "gemini-2.0-flash-lite",
    }

    chosen = model_map.get(cq.data)
    set_model(cq.from_user.id, chosen)

    await cq.message.edit_text(
        f"✅ **Model Updated Successfully!**\n\nYour new model is:\n**{chosen}**",
        reply_markup=ai_model_keyboard()
    )

# -------------------------------
# Commands
# -------------------------------


def get_greeting_ist():
    ist = timezone("Asia/Kolkata")
    now = datetime.now(ist)
    hour = now.hour

    if 5 <= hour < 12:
        return "🌅 **Good Morning!**"
    elif 12 <= hour < 17:
        return "🌞 **Good Afternoon!**"
    elif 17 <= hour < 21:
        return "🌆 **Good Evening!**"
    else:
        return "🌙 **Good Night!**"

# --------------------------------------------------------------

@app.on_message(filters.command("start"))
async def start(_, msg):
    try:
        user = get_user(msg.from_user.id)

        # Save user basic info
        if msg.from_user.username:
            set_user_field(msg.from_user.id, "username", msg.from_user.username)
        if msg.from_user.first_name:
            set_user_field(msg.from_user.id, "name", msg.from_user.first_name)

        greeting = get_greeting_ist()

        await msg.reply(
            f"{greeting} 👋\n"
            f"**Welcome to Gemini Ask Bot!** 🚀\n\n"
            "💡 **You can ask questions in 3 ways:**\n\n"
            "• `/ask upsc full form ` — **instant ask**\n"
            "• Reply to any message with `/ask` — **smart reply**\n"
            "• Send `/ask` alone — **I'll wait for your question** 🎯\n\n"
            "📚 Use **`/help`** for full information and commands.",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        await send_error_traceback(e, "start handler")

@app.on_message(filters.command("admin_settings") & filters.user(OWNER_ID))
async def admin_start(_, msg):
    try:
        await msg.reply(
            "👨‍💼 **Welcome to Admin Panel!** ⚙️\n\n"
            "🔧 **Manage your bot settings here:**\n\n"
            "• **Points Management** 👥\n"
            "• **User Management** 🚫\n"
            "• **API Configuration** 🔑",
            reply_markup=admin_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await send_error_traceback(e, "admin_start")
       
@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    try:
        await msg.reply(
            "📚 **Help & Commands** 📖\n\n"
            "🔥 **Quick Commands:**\n\n"
            "• `/ask <question>` — **Ask instantly** ⚡\n"
            "• Reply + `/ask` — **Ask about replied message** 👆\n"
            "• `/ask` alone — **Interactive mode** 💬\n"
            "• `/restart` — **Clear pending request** 🔄\n"
            "• `/stats` — **Your usage statistics** 📊\n\n"
            "👑 **Owner Commands:**\n"
            "• Press **Admin** button or `/admin_settings`\n\n"
            "⚙️ **Bot Commands:**\n"
            "• `/setcommands` — **Register bot commands** ✅",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await send_error_traceback(e, "help handler")

@app.on_message(filters.command("setcommands"))
async def setcommands(_, msg):
    try:
        # register commands (friendly list)
        cmds = [
            ("start", "🚀 Start / welcome"),
            ("ask", "❓ Ask a question"),
            ("stats", "📊 Your usage statistics"),
            ("bonus", "⭐ Daily bonus points"),
            ("balance", "⚖️ Check your points"),
            ("help", "📚 Show help"),
        ]
        if msg.from_user.id == OWNER_ID:
            cmds += [
                ("admin_settings", "⚙️ Open admin menu"),
                ("set_api", "🔑 Set Gemini API key"),
            ]
        await app.set_bot_commands([{"command": c, "description": d} for c, d in cmds])
        await msg.reply("✅ **Bot commands updated successfully!** 🎉", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await send_error_traceback(e, "setcommands")

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
            return await msg.reply("❌ **Access Denied!** 🔒\n\n**Only the owner can use this command.**", parse_mode=ParseMode.MARKDOWN)
        if len(msg.command) > 1:
            key = " ".join(msg.command[1:]).strip()
        else:
            asked = await app.ask(msg.chat.id, text="🔑 **Send the Gemini API key:**\n\n**Or type `/cancel` to cancel:**")
            key = (asked.text or "").strip()
        if not key:
            return await msg.reply("❌ **No key provided!** Operation cancelled.", parse_mode=ParseMode.MARKDOWN)
        set_setting("gemini_api_key", key)
        set_gemini_client(key)
        await msg.reply("✅ **Gemini API key saved and applied successfully!** 🎉\n\n**Bot is ready to answer questions!** 🚀", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await send_error_traceback(e, "set_api_cmd")

# -------------------------------


THINKING_FRAMES = [
    "⏳ **thinking…**",
    "⌛ **thinking** 🤔",
    "⏳ **thinking** 🤔🤔",
    "⌛ **thinking** 🤔🤔🤔",
    "⏳ **thinking** 🧐",
    "⌛ **thinking** 🧐🧐",
    "⏳ **thinking** 🧐🧐🧐",
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
            await message.edit_text(frame, parse_mode=ParseMode.MARKDOWN)
            i += 1
            await asyncio.sleep(0.7) # speed of animation
    except Exception as e:
        await send_error_traceback(e, "query_gemini in ask_handler")

# Main Ask handler
# -------------------------------
@app.on_message(filters.command("ask"))
async def ask_handler(app_obj, msg):
    try:
        uid = msg.from_user.id

        # Get user data from MongoDB
        user = get_user(uid)
        
        if not user:
            welcome_points = WELCOME_POINT  # 50 points
            inc_user_field(uid, "points", welcome_points)
        
            await msg.reply(
                "🎉 **Welcome to the bot!**\n"
                "You’ve received **✨ 50 FREE welcome points! ✨**\n\n"
                "💡 **How it works:**\n"
                "• Each query costs **1 point**.\n"
                "• Use your points wisely to ask high-quality questions. 🤖💬\n\n"
                "⭐ Want more points?\n"
                "Use **/bonus** to claim free points.\n"
                "Check your balance anytime with **/balance**.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        points = user.get("points", 0)

        # Check minimum required points
        if points < ASK_MIN_POINTS:
            return await msg.reply(
                f"❌ **Insufficient Points!** 💸\n\n"
                f"📊 **Required:** `{ASK_MIN_POINTS}` points\n"
                f"💰 **Your balance:** `{points}` points\n\n"
                f"💡 **Get free points:**\n"
                f"• `/bonus` — **Daily bonus** ⭐\n"
                f"• `/balance` — **Check balance** ⚖️",
                parse_mode=ParseMode.MARKDOWN
            )

        # ----- USER HAS ENOUGH POINTS → CONTINUE WITH ASK LOGIC -----

        if user.get("banned"):
            return await msg.reply("🚫 **You are banned from using this bot!** 🔒\n\n**Contact admin for assistance.**", parse_mode=ParseMode.MARKDOWN)
           
        # 1) inline: /ask question
        if len(msg.command) > 1:
            question = " ".join(msg.command[1:])
           
        # 2) replied message
        elif msg.reply_to_message:
            rep = msg.reply_to_message
            if rep.poll:
                poll = rep.poll
        
                question = poll.question
                options = [opt.text for opt in poll.options]
        
                correct_answer = None
                if poll.type == "quiz":
                    try:
                        correct_answer = options[poll.correct_option_id]
                    except:
                        correct_answer = "Not specified"
        
                # Build prompt for AI
                text = f"""
You have to solve a quiz.
        
Question:{question}
        
Options:
{chr(10).join([f"{i+1}. {opt}" for i,opt in enumerate(options)])}
        
Correct Answer Provided:{correct_answer}
        
Now explain the answer in simple words and give the final result.
"""
            else:
                text = (rep.text or rep.caption or "").strip()
            
            if not text:
                return await msg.reply("⚠️ **Replied message has no text!** 📝\n\n**Please reply to a message with text or caption.**", parse_mode=ParseMode.MARKDOWN)
            question = text
               
        # 3) interactive ask
        else:
            asked = await app.ask(msg.chat.id, text="✍️ **Send your question:**\n\n**💡 Tip:** Be specific for better answers!\n**🚫 Or type `/cancel` to cancel:**")
            question = (asked.text or "").strip()
            if not question:
                return await asked.reply("❌ **No question provided!** Operation cancelled. 😕", parse_mode=ParseMode.MARKDOWN)
               
        status = await msg.reply("⏳ **processing your question…**", parse_mode=ParseMode.MARKDOWN)
       
        try:
            animation_task = asyncio.create_task(animate_status(status))
        except Exception as e:
            await send_error_traceback(e, "query_gemini in ask_handler")
           
        try:
            # Stop animation
            try:
                answer = await query_gemini(question)
            finally:
                try:
                    animation_task.cancel()
                except Exception as e:
                    await send_error_traceback(e, "query_gemini in ask_handler")
                   
            answer = await query_gemini(question)
        except Exception as e:
            await status.edit_text(f"⚠️ **Error while querying Gemini!** 😞\n\n**Error:** `{str(e)[:100]}`\n\n**Please try again later or contact admin.**", parse_mode=ParseMode.MARKDOWN)
            await send_error_traceback(e, "query_gemini in ask_handler")
            return
        # increment stats in DB
        inc_user_field(uid, "stats", 1)
        inc_user_field(uid, "points", -1)
        total = get_setting("total_questions") or 0
        set_setting("total_questions", (total or 0) + 1)
        # deliver answer (chunked)
        await status.edit_text(f"❓ **Question:**\n`{question}`\n\n💡 **Answer:**", parse_mode=ParseMode.MARKDOWN)
        for chunk in chunk_text(answer):
            await app.send_message(msg.chat.id, chunk, parse_mode=ParseMode.MARKDOWN)
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
            return await message.reply_text(f"🟦 **You typed:**\n`{user_text}`", parse_mode=ParseMode.MARKDOWN)
        if message.reply_to_message:
            replied = message.reply_to_message
            if getattr(replied, "text", None):
                return await message.reply_text(f"🟧 **Replied text:**\n`{replied.text}`", parse_mode=ParseMode.MARKDOWN)
            elif getattr(replied, "caption", None):
                return await message.reply_text(f"🟨 **Replied caption:**\n`{replied.caption}`", parse_mode=ParseMode.MARKDOWN)
            else:
                return await message.reply_text("⚠️ **Replied message has no text/caption!** 📝", parse_mode=ParseMode.MARKDOWN)
        ask_msg = await app.ask(chat_id=message.chat.id, text="👀 **Send me something to display:**")
        await ask_msg.reply_text(f"🟩 **You typed:**\n`{ask_msg.text}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await send_error_traceback(e, "see_cmd")

# -------------------------------
# /bonus (daily)
# -------------------------------
@app.on_message(filters.command("bonus"))
async def bonus_cmd(_, msg):
    try:
        uid = msg.from_user.id
        user = get_user(uid) # pymongo user getter

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
            return await msg.reply(
                f"⏳ **Daily bonus already claimed!** 🎁\n\n"
                f"⏰ **Next bonus available in:**\n"
                f"`{hours}h {minutes}m`\n\n"
                f"💡 **Tip:** Check back tomorrow for more free points! ⭐",
                parse_mode=ParseMode.MARKDOWN
            )

        # -------- GIVE BONUS POINTS -------- #
        points = BONUS_POINTS
        inc_user_field(uid, "points", points) # add points
        set_user_field(uid, "last_bonus", now.isoformat())

        await msg.reply(
            f"🎉 **Congratulations!** 🥳\n\n"
            f"⭐ **You received `{points}` bonus points!** ✨\n\n"
            f"💰 **Check your balance:** `/balance`\n"
            f"❓ **Ask questions:** `/ask`",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        await send_error_traceback(e, "bonus_cmd")
       
@app.on_message(filters.command("balance"))
async def balance_cmd(_, message):
    try:
        uid = message.from_user.id

        # Fetch user document via your helper
        user = get_user(uid)

        # If user does not exist → create default
        if not user:
            user = {"_id": uid, "points": 0, "stats": 0, "banned": False}
            users_col.insert_one(user)

        points = user.get("points", 0)

        await message.reply_text(
            f"⚖️ **Your Balance** 💰\n\n"
            f"⭐ **Points:** `{points}`\n\n"
            f"💡 **Commands:**\n"
            f"• `/bonus` — **Get daily bonus** ⭐\n"
            f"• `/ask` — **Ask question** (1 point)\n"
            f"• `/stats` — **View statistics** 📊",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        await send_error_traceback(e, "balance_cmd")


@app.on_message(filters.command("stats"))
async def stats_cmd(app, message):
    try:

        start_ping = time.time()
   
        # --- Calculate API latency (ping)
        pong_msg = await message.reply("⏱️ **Calculating ping…**", parse_mode=ParseMode.MARKDOWN)
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
            f"📊 **Bot Statistics** 📈\n\n"
            f"👤 **Your Statistics:**\n"
            f"• **Questions Asked:** `{user.get('stats', 0)}` ❓\n"
            f"• **Points Balance:** `{user.get('points', 0)}` ⭐\n\n"
            f"🌍 **Global Statistics:**\n"
            f"• **Total Users:** `{total_users}` 👥\n"
            f"• **Total Questions:** `{total_questions}` ❓\n\n"
            f"🖥️ **System Info:**\n"
            f"• **Uptime:** `{uptime_str}` ⏰\n"
            f"• **Response Time:** `{ping_ms:.2f} ms` ⚡",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await send_error_traceback(e, "stats")

@app.on_message(filters.command(["restart"]) & filters.user(OWNER_ID))
async def restart_handler(_, m):
    await m.reply_text("🔄 **Bot is Restarting…** 🚀\n\n**Please wait 5-10 seconds…**", parse_mode=ParseMode.MARKDOWN, disable_notification=True)
    os.execl(sys.executable, sys.executable, *sys.argv)

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
                await cq.answer("🔒 **Access Denied! Owner only**", show_alert=True)
                return
            await cq.message.edit_text("👨‍💼 **Admin Panel** ⚙️\n\n**Manage your bot settings:**", reply_markup=admin_keyboard(), parse_mode=ParseMode.MARKDOWN)
            await cq.answer()
            return
        # Return to main admin + back
        if data == "admin_back":
            await cq.message.edit_text("🏠 **Main Menu** 🎉", reply_markup=main_keyboard(), parse_mode=ParseMode.MARKDOWN)
            await cq.answer()
            return
        # Add points
        if data == "admin_add_points":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            asked = await app.ask(uid, "➕ **Add Points**\n\n**Format:**\n• `<user_id> <amount>`\n• **OR** reply to message + `<amount>`")
            target, amount = _parse_target_and_amount_from_text(asked.text or "", asked.reply_to_message)
            if target is None:
                return await app.send_message(uid, "❌ **Invalid format!** 😕\n\n**Correct formats:**\n• `123456 50`\n• Reply + `50`", parse_mode=ParseMode.MARKDOWN)
            inc_user_field(target, "points", amount)
            await app.send_message(uid, f"✅ **Points Added Successfully!** 🎉\n\n👤 **User ID:** `{target}`\n➕ **Amount:** `+{amount}` points\n💰 **Operation completed!**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # Remove points
        if data == "admin_rem_points":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            asked = await app.ask(uid, "➖ **Remove Points**\n\n**Format:**\n• `<user_id> <amount>`\n• **OR** reply to message + `<amount>`")
            target, amount = _parse_target_and_amount_from_text(asked.text or "", asked.reply_to_message)
            if target is None:
                return await app.send_message(uid, "❌ **Invalid format!** 😕\n\n**Correct formats:**\n• `123456 50`\n• Reply + `50`", parse_mode=ParseMode.MARKDOWN)
            inc_user_field(target, "points", -abs(amount))
            await app.send_message(uid, f"✅ **Points Removed Successfully!** ✅\n\n👤 **User ID:** `{target}`\n➖ **Amount:** `-{abs(amount)}` points\n💰 **Operation completed!**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # Ban user
        if data == "admin_ban":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            asked = await app.ask(uid, "🚫 **Ban User**\n\n**Format:**\n• `<user_id>`\n• **OR** reply to user's message")
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
                return await app.send_message(uid, "❌ **Invalid user ID!** 😕\n\n**Please provide a valid user ID.**", parse_mode=ParseMode.MARKDOWN)
            set_user_field(target_id, "banned", True)
            await app.send_message(uid, f"🚫 **User Banned Successfully!** 🔒\n\n👤 **User ID:** `{target_id}`\n⛔ **Status:** **BANNED**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # Unban user
        if data == "admin_unban":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            asked = await app.ask(uid, "✅ **Unban User**\n\n**Format:**\n• `<user_id>`\n• **OR** reply to user's message")
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
                return await app.send_message(uid, "❌ **Invalid user ID!** 😕\n\n**Please provide a valid user ID.**", parse_mode=ParseMode.MARKDOWN)
            set_user_field(target_id, "banned", False)
            await app.send_message(uid, f"✅ **User Unbanned Successfully!** 🎉\n\n👤 **User ID:** `{target_id}`\n✅ **Status:** **UNBANNED**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # API menu
        if data == "admin_api_menu" or data == "api_menu":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            await cq.message.edit_text("🔑 **API Key Management** 🔧\n\n**Manage Gemini API configuration:**", reply_markup=api_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # API set
        if data == "api_set":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            asked = await app.ask(uid, "🔧 **Set Gemini API Key**\n\n**Please send your Gemini API key:**\n\n**💡 Tip:** Get it from [Google AI Studio](https://makersuite.google.com/app/apikey)")
            key = (asked.text or "").strip()
            if not key:
                return await app.send_message(uid, "❌ **No API key provided!** Operation cancelled. 😕", parse_mode=ParseMode.MARKDOWN)
            set_setting("gemini_api_key", key)
            set_gemini_client(key)
            await app.send_message(uid, "✅ **API Key Set Successfully!** 🎉\n\n🚀 **Bot is now ready to answer questions!**\n⏰ **Configuration applied immediately.**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # API view
        if data == "api_view":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            key = get_setting("gemini_api_key") or ENV_GEMINI_API_KEY
            if not key:
                return await app.send_message(uid, "❌ **No API key configured!** 😕\n\n**Please set API key using:**\n🔧 **Set API Key** button", parse_mode=ParseMode.MARKDOWN)
            show = ("*" + key[-4:]).rjust(len(key), "•") if len(key) > 4 else ("*" + key)
            await app.send_message(uid, f"🔐 **Current Gemini API Key:**\n\n`{show}`\n\n✅ **Status:** **Active**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # API remove
        if data == "api_remove":
            if uid != OWNER_ID:
                return await cq.answer("🔒 **Owner only!**", show_alert=True)
            set_setting("gemini_api_key", "")
            # also clear runtime
            try:
                set_gemini_client("")
            except Exception:
                pass
            await app.send_message(uid, "🗑️ **API Key Removed Successfully!** ✅\n\n⚠️ **Bot will not work until new API key is set.**", parse_mode=ParseMode.MARKDOWN)
            return await cq.answer()
        # Ask button from main keyboard
        if data == "ask_btn":
            await cq.answer()
            uid = cq.from_user.id
       
            # Step 1: Ask the user
            asked = await app.ask(
                uid,
                text="✍️ **Ask your question:**\n\n💡 **Tips for better answers:**\n• Be specific and clear\n• Provide context when needed\n• Ask one question at a time\n\n🚫 **Type `/cancel` to cancel:**",
                parse_mode=ParseMode.MARKDOWN
            )
       
            question = (asked.text or "").strip()
            if not question:
                return await asked.reply("❌ **No question provided!** Operation cancelled. 😕", parse_mode=ParseMode.MARKDOWN)
       
            # Step 2: Send thinking message
            status = await asked.reply("⏳ **processing your question…**", parse_mode=ParseMode.MARKDOWN)
       
            # Step 3: Start animation
            animation_task = asyncio.create_task(animate_status(status))
       
            # Step 4: Query Gemini safely
            try:
                answer = await query_gemini(question)
            except Exception as e:
                animation_task.cancel()
                await status.edit_text(f"⚠️ **Error while processing!** 😞\n\n**Error:** `{str(e)[:100]}`\n\n**Please try again or contact admin.**", parse_mode=ParseMode.MARKDOWN)
                await send_error_traceback(e, "query_gemini in ask_btn")
                return
            finally:
                animation_task.cancel()
       
            # Step 5: Update stats
            inc_user_field(uid, "stats", 1)
            inc_user_field(uid, "points", -1)
            total = get_setting("total_questions") or 0
            set_setting("total_questions", total + 1)
       
            # Step 6: Send final answer
            await status.edit_text(f"❓ **Question:**\n`{question}`\n\n💡 **Answer:**", parse_mode=ParseMode.MARKDOWN)
            for chunk in chunk_text(answer):
                await app.send_message(cq.message.chat.id, chunk, parse_mode=ParseMode.MARKDOWN)
       
            # Step 7: Log Q&A
            try:
                set_user_field(uid, "username", cq.from_user.username or "")
                set_user_field(uid, "name", cq.from_user.first_name or "")
                await log_qa(get_user(uid), question, answer)
            except Exception as e:
                await send_error_traceback(e, "log_qa")
       
            return

        # Stats button
        if data == "stats_btn":
            u = get_user(uid)
            await cq.answer()
            await cq.message.reply(f"📊 **Your Statistics:**\n\n❓ **Questions Asked:** `{u.get('stats',0)}`\n⭐ **Points:** `{u.get('points',0)}`", parse_mode=ParseMode.MARKDOWN)
            return
        # Bonus button
        if data == "bonus_btn":
            await cq.answer()
            return await bonus_cmd(None, cq.message) # reuse bonus logic
        # Balance button
        if data == "balance_btn":
            await cq.answer()
            return await balance_cmd(None, cq.message) # reuse balance logic
        # fallback
        await cq.answer()
    except Exception as e:
        await send_error_traceback(e, "callback_query_handler")


# -------------------------------
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_ID,
        "text": "🚀 **Bot Restarted Successfully!** ✅\n\n📊 **Status:** **Online**\n⏰ **Time:** " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "parse_mode": "Markdown"
    }
    requests.post(url, data=data)
# -------------------------------


def reset_and_set_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"

    # General users ke liye commands
    general_commands = [
        {"command": "start", "description": "🚀 Start / welcome"},
        {"command": "cancel", "description": "🚫 Stop the ongoing process"},
        {"command": "id", "description": "🆔 Get Your ID"},
        {"command": "ask", "description": "❓ Ask a question"},
        {"command": "bonus", "description": "⭐ Get free points"},
        {"command": "balance", "description": "⚖️ View your points"},
    ]
    # Owner ke liye extra commands
    owner_commands = general_commands + [
        {"command": "broadcast", "description": "📢 Broadcast to All Users"},
        {"command": "broadusers", "description": "👥 All Broadcasting Users"},
        {"command": "add_user", "description": "▶️ Add Authorisation"},
        {"command": "rem_user", "description": "⏸️ Remove Authorisation"},
        {"command": "set_api", "description": "🔑 AI API key"},
        {"command": "admin_settings", "description": "⚙️ Admin settings"},
        {"command": "restart", "description": "🔄 Restart the Bot"},
        {"command": "stats", "description": "📊 Bot statistics"}
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
        "scope": {"type": "chat", "chat_id": OWNER_ID},
        "language_code": "en"
    })

# -------------------------------
# Run bot
# -------------------------------
if __name__ == "__main__":
    try:
        notify_owner()
        print("✅ Owner notified successfully!")
    except Exception as e:
        print("⚠️ Failed notify_owner handler:", e)
       
    try:
        reset_and_set_commands()
        print("✅ Bot commands updated successfully!")
    except Exception as e:
        print("⚠️ Failed to reset_and_set_commands handler:", e)

    print("🚀 **Starting Gemini Ask Bot...**")
    print("📊 **Features:** Points System | Admin Panel | Gemini AI | MongoDB")
    print("🌐 **Bot is now online!** ✅")
    
    app.run()
