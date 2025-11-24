import os
import textwrap
import asyncio
import requests
from collections import defaultdict
from datetime import datetime, timezone, date
from dotenv import load_dotenv

from pyrofork import Client, filters
from pyrofork.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai

# Optional async MongoDB driver
try:
    import motor.motor_asyncio as motor
except ImportError:
    motor = None  # If you don't need MongoDB, keep it None

# Load env variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")  # mongodb+srv://... or mongodb://...
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
BOT_USERNAME = os.getenv("BOT_USERNAME")  # for referral links, e.g. MyBot

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("BOT_TOKEN, API_ID, and API_HASH must be defined in .env")

# Initialize bot
app = Client(
    name="ask_bot",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH,
)

# Gemini AI client
gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# DB setup (optional)
if MONGO_URI and motor:
    mongo_client = motor.AsyncIOMotorClient(MONGO_URI)
    db = mongo_client.get_default_database()
    users_col = db.get_collection("users")
    sessions_col = db.get_collection("sessions")
else:
    mongo_client = None
    db = None
    users_col = None
    sessions_col = None

# In-memory state (fallback if no DB)
user_states = defaultdict(lambda: {"awaiting": False, "last_answer": None})
user_stats = defaultdict(int)
total_stats = 0

# Uptime tracking
START_TIME = datetime.now(timezone.utc)

# Helper: inline main keyboard
def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ask a question", callback_data="ask_btn")],
            [
                InlineKeyboardButton(text="Help", callback_data="help_btn"),
                InlineKeyboardButton(text="Stats", callback_data="stats_btn"),
            ],
        ]
    )

# Follow-up buttons to be shown after each answer
def followup_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Explain more", callback_data="follow_explain"),
                InlineKeyboardButton(text="Summarize", callback_data="follow_summarize"),
            ],
            [InlineKeyboardButton(text="Give code example", callback_data="follow_code")],
        ]
    )

# Utility: chunk text for long messages
def chunk_text(text, size=3800):
    for i in range(0, len(text), size):
        yield text[i:i+size]

# Gemini query wrapper (runs in executor to avoid blocking)
async def query_gemini(prompt: str):
    if not gemini:
        raise RuntimeError("Gemini API key not configured")

    loop = asyncio.get_event_loop()

    def _call():
        res = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return getattr(res, "text", None) or str(res)

    return await loop.run_in_executor(None, _call)

# Database helpers for user memory/points/ban and referrals
async def ensure_user_in_db(uid: int, start_param: str | None = None):
    if users_col is None:
        # ensure in-memory defaults
        st = user_states[uid]
        st.setdefault("points", 50)
        st.setdefault("banned", False)
        st.setdefault("ban_reason", None)
        st.setdefault("referrals", 0)
        return st

    u = await users_col.find_one({"user_id": uid})
    if not u:
        now = datetime.now(timezone.utc)
        user_doc = {
            "user_id": uid,
            "points": 50,           # new user gets 50 points
            "banned": False,
            "ban_reason": None,
            "memory": "",
            "referrals": 0,
            "last_refill": now.isoformat(),
        }
        # Check start_param for referral
        if start_param and start_param.startswith("ref"):
            try:
                ref_id = int(start_param.replace("ref", ""))
                user_doc["referred_by"] = ref_id
            except Exception:
                user_doc["referred_by"] = None
        await users_col.insert_one(user_doc)
        # If referred, award points to referrer
        if user_doc.get("referred_by"):
            ref = user_doc["referred_by"]
            await users_col.find_one_and_update({"user_id": ref}, {"$inc": {"points": 5, "referrals": 1}})
        return user_doc
    return u

async def get_user_doc(uid: int):
    if users_col is None:
        return ensure_user_in_db(uid)
    return await ensure_user_in_db(uid)

async def change_points(uid: int, delta: int):
    if users_col is None:
        # fallback to in-memory
        user_states[uid].setdefault("points", 50)
        user_states[uid]["points"] += delta
        return user_states[uid]["points"]
    res = await users_col.find_one_and_update(
        {"user_id": uid},
        {"$inc": {"points": delta}},
        return_document=True
    )
    return res["points"] if res else None

async def set_ban(uid: int, banned: bool, reason: str | None = None):
    if users_col is None:
        user_states[uid]["banned"] = banned
        user_states[uid]["ban_reason"] = reason
        return
    await users_col.update_one({"user_id": uid}, {"$set": {"banned": banned, "ban_reason": reason}})

async def get_points(uid: int):
    if users_col is None:
        return user_states[uid].get("points", 50)
    u = await users_col.find_one({"user_id": uid})
    return u["points"] if u else None

async def refill_daily_bonus(uid: int):
    """Add daily bonus (20) once per UTC day. Returns new points or None."""
    if users_col is None:
        st = user_states[uid]
        today = date.today()
        if st.get("last_refill") != today.isoformat():
            st["last_refill"] = today.isoformat()
            st.setdefault("points", 50)
            st["points"] += 20
        return st["points"]

    now = datetime.now(timezone.utc)
    u = await ensure_user_in_db(uid)
    last = u.get("last_refill")
    last_date = None
    if last:
        last_date = date.fromisoformat(last.split("T")[0])
    if last_date != now.date():
        res = await users_col.find_one_and_update(
            {"user_id": uid},
            {"$inc": {"points": 20}, "$set": {"last_refill": now.isoformat()}},
            return_document=True,
        )
        return res["points"] if res else None
    return u["points"]

# Utility: check ban and points before allowing an ask
async def can_ask(uid: int):
    # refill bonus if eligible
    await refill_daily_bonus(uid)
    u = await get_user_doc(uid)
    if u and isinstance(u, dict) and u.get("banned"):
        return False, f"You are banned. Reason: {u.get('ban_reason') or 'No reason given.'}"
    pts = await get_points(uid)
    if pts is not None and pts <= 0:
        return False, "You have no points left. Come back tomorrow for bonus or ask admin to add points."
    return True, None

# Format helpful markdown reply header
def format_header(question: str):
    return f"""**Question:**
> {textwrap.shorten(question, width=400, placeholder='...')}

**Answer:**"""

# Commands to set bot commands and notify owner
def reset_and_set_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    # Reset
    requests.post(url, json={"commands": []})
    # Set new
    commands = [
        {"command": "start", "description": "✅ ᴄʜᴇᴄᴋ ɪꜰ ᴛʜᴇ ʙᴏᴛ ɪꜱ ᴀʟɪᴠᴇ"},
        {"command": "drm", "description": "📄 ᴜᴘʟᴏᴀᴅ ᴀ .ᴛxᴛ ꜰɪʟᴇ"},
        {"command": "stop", "description": "⏹️ ᴛᴇʀᴍɪɴᴀᴛᴇ ᴛʜᴇ ᴏɴɢᴏɪɴɢ ᴘʀᴏᴄᴇꜱꜱ"},
        {"command": "reset", "description": "♻️ ʀᴇꜱᴇᴛ ᴛʜᴇ ʙᴏᴛ"},
        {"command": "cookies", "description": "🍪 ᴜᴘʟᴏᴀᴅ ʏᴏᴜᴛᴜʙᴇ ᴄᴏᴏᴋɪᴇꜱ"},
        {"command": "t2t", "description": "📝 ᴛᴇxᴛ → .ᴛxᴛ ɢᴇɴᴇʀᴀᴛᴏʀ"},
        {"command": "id", "description": "🆔 ɢᴇᴛ ʏᴏᴜʀ ᴜꜱᴇʀ ɪᴅ"},
        {"command": "logs", "description": "👁 ᴠɪᴇᴡ ʙᴏᴛ ᴀᴄᴛɪᴠɪᴛʏ"},
        {"command": "plan", "description": "⏸️ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ ᴘʟᴀɴ"},
        {"command": "refer", "description": "🔗 ɢᴇᴛ ʏᴏᴜʀ ʀᴇꜰᴇʀʀᴀʟ ʟɪɴᴋ"},
        {"command": "balance", "description": "💳 ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴘᴏɪɴᴛs"},
        {"command": "ask", "description": "💬 ᴀsᴋ ɢᴇᴍɪɴɪ ᴀɪ ᴀ ǫᴜᴇsᴛɪᴏɴ"},
    ]

    requests.post(url, json={"commands": commands})


def notify_owner():
    if not OWNER_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_ID,
        "text": "BOT is Live Now 🤖"
    }
    try:
        requests.post(url, data=data, timeout=5)
    except Exception:
        pass

# Startup helpers
def startup_tasks():
    try:
        reset_and_set_commands()
    except Exception:
        pass
    try:
        notify_owner()
    except Exception:
        pass

# Admin utility - naive permission check (replace with real admin list)
def is_admin(uid: int):
    admin_ids = set(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else set()
    return uid in admin_ids

# Commands & handlers
@app.on_message(filters.command("start"))
async def start(_, msg):
    # Handle referral parameter: /start ref<user_id>
    start_param = None
    if len(msg.command) > 1:
        start_param = msg.command[1]

    uid = msg.from_user.id
    # Ensure user exists in DB and handle referral if any
    try:
        await ensure_user_in_db(uid, start_param=start_param)
    except Exception:
        pass

    # If user was referred, try to notify both parties
    if start_param and start_param.startswith("ref"):
        try:
            ref_id = int(start_param.replace("ref", ""))
            # awarded in ensure_user_in_db when DB is used; for in-memory, handle here
            if users_col is None:
                user_states[ref_id].setdefault("points", 50)
                user_states[ref_id]["points"] += 5
                user_states[ref_id].setdefault("referrals", 0)
                user_states[ref_id]["referrals"] += 1
            # notify referrer
            try:
                await app.send_message(ref_id, f"🎉 You earned 5 points! A new user joined using your referral.")
            except Exception:
                pass
        except Exception:
            pass

    await msg.reply(
        "👋 *Welcome to Gemini AI Ask Bot*"
        "You can ask questions in 3 ways:"
        "• `/ask your question`"
        "• Reply to any message with `/ask`"
        "• Send `/ask` alone and I will wait for your question"
        "Use /help for full info.",
        reply_markup=main_keyboard()
    )

@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    await msg.reply(
        "*Commands*"
        "• `/ask <question>` — Ask instantly"
        "• Reply to a message with `/ask`"
        "• `/ask` alone — I wait for your next message"
        "• `/restart` — Clear pending request"
        "• `/stats` — Show usage statistics and uptime"
        "• `/balance` — Show your points balance"
        "• `/refer` — Get your referral link to earn 5 points per referral"
        "Admin-only: `/add_points <user_id> <amount>`, `/remove_points <user_id> <amount>`, `/ban <user_id> [reason]`, `/unban <user_id>`"
    )

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    uid = msg.from_user.id
    user_states[uid]["awaiting"] = False
    await msg.reply("🔄 Session restarted. Use /ask to ask a question again.")

@app.on_message(filters.command("stats"))
async def stats(_, msg):
    uid = msg.from_user.id
    uptime = datetime.now(timezone.utc) - START_TIME
    # human-friendly uptime
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    await msg.reply(
        f"*Your Stats:*"
        f"Questions asked: {user_stats[uid]}"
        f"*Total Questions:* {total_stats}"
        f"*Uptime:* {hours}h {minutes}m {seconds}s"
    )

@app.on_message(filters.command("balance"))
async def balance_cmd(_, msg):
    uid = msg.from_user.id
    pts = await get_points(uid)
    await msg.reply(f"💳 Your points: {pts}")

@app.on_message(filters.command("refer"))
async def refer_cmd(_, msg):
    uid = msg.from_user.id
    # Build referral link
    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
    else:
        link = f"Use this command to invite users. Provide BOT_USERNAME in .env to get a full link. Your ref code: ref{uid}"

    # get referral count if available
    if users_col is None:
        refs = user_states[uid].get("referrals", 0)
    else:
        u = await users_col.find_one({"user_id": uid})
        refs = u.get("referrals", 0) if u else 0

    await msg.reply(f"🔗 Your referral link: {link}\nReferrals: {refs}\nEarn 5 points for each new user who starts with your link.")

# Admin utility commands
@app.on_message(filters.command("add_points"))
async def add_points_cmd(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ You are not authorized to use this command.")
    if len(msg.command) < 3:
        return await msg.reply("Usage: /add_points <user_id> <amount>")
    target = int(msg.command[1])
    amount = int(msg.command[2])
    new_pts = await change_points(target, amount)
    await msg.reply(f"✅ Added {amount} points to {target}. New balance: {new_pts}")

@app.on_message(filters.command("remove_points"))
async def remove_points_cmd(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ You are not authorized to use this command.")
    if len(msg.command) < 3:
        return await msg.reply("Usage: /remove_points <user_id> <amount>")
    target = int(msg.command[1])
    amount = int(msg.command[2])
    new_pts = await change_points(target, -abs(amount))
    await msg.reply(f"✅ Removed {amount} points from {target}. New balance: {new_pts}")

@app.on_message(filters.command("ban"))
async def ban_cmd(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ You are not authorized to use this command.")
    if len(msg.command) < 2:
        return await msg.reply("Usage: /ban <user_id> [reason]")
    target = int(msg.command[1])
    reason = " ".join(msg.command[2:]) if len(msg.command) > 2 else None
    await set_ban(target, True, reason)
    await msg.reply(f"🚫 User {target} banned. Reason: {reason or 'No reason provided.'}")

@app.on_message(filters.command("unban"))
async def unban_cmd(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("❌ You are not authorized to use this command.")
    if len(msg.command) < 2:
        return await msg.reply("Usage: /unban <user_id>")
    target = int(msg.command[1])
    await set_ban(target, False, None)
    await msg.reply(f"✅ User {target} unbanned.")

# Core ask handler
@app.on_message(filters.command("ask"))
async def ask(_, msg):
    global total_stats
    uid = msg.from_user.id

    # Check ban / points
    ok, reason = await can_ask(uid)
    if not ok:
        return await msg.reply(f"⚠️ {reason}")

    # Determine question text
    if len(msg.command) > 1:
        question = " ".join(msg.command[1:])
    elif msg.reply_to_message and msg.reply_to_message.text:
        question = msg.reply_to_message.text
    else:
        user_states[uid]["awaiting"] = True
        return await msg.reply("✍️ Please send your question...")

    # Deduct one point
    await change_points(uid, -1)

    user_states[uid]["awaiting"] = False
    user_stats[uid] += 1
    total_stats += 1

    status = await msg.reply("⏳ Thinking...")

    try:
        answer = await query_gemini(question)
    except Exception as e:
        return await status.edit_text(f"⚠️ Error: {e}")

    # Save last answer for follow-ups (in-memory + optional DB session)
    last = {"question": question, "answer": answer}
    user_states[uid]["last_answer"] = last
    if sessions_col is not None:
        await sessions_col.update_one({"user_id": uid}, {"$set": {"last_answer": last, "updated": datetime.now(timezone.utc)}}, upsert=True)

    # Build header and send first chunk by editing status to reduce message spam
    header = format_header(question)
    chunks = list(chunk_text(answer))
    first_chunk = chunks[0] if chunks else "(No answer)"
    await status.edit_text(header + "
" + first_chunk, reply_markup=followup_keyboard())

    # Send remaining chunks as separate replies
    for chunk in chunks[1:]:
        await msg.reply(chunk)


# Catch plain text when awaiting
@app.on_message(filters.text)
async def catch_text(_, msg):
    uid = msg.from_user.id

    if user_states[uid]["awaiting"]:
        user_states[uid]["awaiting"] = False

        ok, reason = await can_ask(uid)
        if not ok:
            return await msg.reply(f"⚠️ {reason}")

        # Deduct point
        await change_points(uid, -1)

        global total_stats
        user_stats[uid] += 1
        total_stats += 1

        status = await msg.reply("⏳ Thinking...")

        try:
            answer = await query_gemini(msg.text)
        except Exception as e:
            return await status.edit_text(f"⚠️ Error: {e}")

        last = {"question": msg.text, "answer": answer}
        user_states[uid]["last_answer"] = last
        if sessions_col is not None:
            await sessions_col.update_one({"user_id": uid}, {"$set": {"last_answer": last, "updated": datetime.now(timezone.utc)}}, upsert=True)

        header = format_header(msg.text)
        chunks = list(chunk_text(answer))
        first_chunk = chunks[0] if chunks else "(No answer)"
        await status.edit_text(header + " " + first_chunk, reply_markup=followup_keyboard())
        for chunk in chunks[1:]:
            await msg.reply(chunk)

    else:
        await msg.reply(
            "Use /ask to ask a question or reply to a message with /ask.",
            reply_markup=main_keyboard()
        )

# Callback queries for main buttons and follow-ups
@app.on_callback_query()
async def callback(_, cq):
    uid = cq.from_user.id
    data = cq.data

    if data == "ask_btn":
        user_states[uid]["awaiting"] = True
        await cq.message.reply("✍️ Send your question now!")
        await cq.answer()

    elif data == "help_btn":
        await cq.message.reply("Use /help to view all commands.")
        await cq.answer()

    elif data == "stats_btn":
        await cq.message.reply(f"You asked {user_stats[uid]} questions total.")
        await cq.answer()

    # Follow-up buttons
    elif data.startswith("follow_"):
        action = data.split("follow_")[1]
        last = user_states[uid].get("last_answer")
        if not last:
            await cq.answer("No recent answer to follow up on.", show_alert=True)
            return
        question = last["question"]

        # Form follow-up prompt variations
        if action == "explain":
            prompt = f"Explain more about the following question and answer. Question: {question}
Previous answer: {last['answer']}. Provide a deeper explanation and examples where useful." 
        elif action == "summarize":
            prompt = f"Provide a concise summary (3-5 lines) of the answer to: {question}
Previous answer: {last['answer']}"
        elif action == "code":
            prompt = f"Give a clear, minimal code example that demonstrates the answer to: {question}
If multiple languages are possible, prefer Python and include brief comments." 
        else:
            await cq.answer()
            return

        await cq.message.reply("⏳ Generating follow-up...")
        await cq.answer()

        try:
            follow_ans = await query_gemini(prompt)
        except Exception as e:
            return await cq.message.reply(f"⚠️ Error: {e}")

        # Save as new last answer
        user_states[uid]["last_answer"] = {"question": question, "answer": follow_ans}
        if sessions_col is not None:
            await sessions_col.update_one({"user_id": uid}, {"$set": {"last_answer": user_states[uid]["last_answer"], "updated": datetime.now(timezone.utc)}}, upsert=True)

        # Reply with follow-up content
        header = f"*Follow-up ({action}):*
> {question}

"
        chunks = list(chunk_text(follow_ans))
        first = chunks[0] if chunks else "(No answer)"
        await cq.message.reply(header + first)
        for chunk in chunks[1:]:
            await cq.message.reply(chunk)

    else:
        await cq.answer()


print("🚀 Gemini Ask Bot Started")
# Run startup tasks synchronously before app.run
startup_tasks()
app.run()
