import os
import textwrap
import asyncio
import requests
from collections import defaultdict
from datetime import datetime, timezone, date
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from google import genai

# Optional async MongoDB
try:
    import motor.motor_asyncio as motor
    mongo_available = True
except ImportError:
    mongo_available = False

load_dotenv()

# === ENV VARIABLES ===
AI_BOT_TOKEN = os.getenv("AI_BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip("@")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not all([AI_BOT_TOKEN, API_ID, API_HASH]):
    raise RuntimeError("Missing required .env variables: AI_BOT_TOKEN, API_ID, API_HASH")

# === CLIENT SETUP ===
app = Client("gemini_ask_bot", bot_token=AI_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

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
    
# === DATABASE SETUP ===
if MONGO_URI and mongo_available:
    mongo_client = motor.AsyncIOMotorClient(MONGO_URI)
    db = mongo_client["gemini_bot"]
    users_col = db.users
    sessions_col = db.sessions
else:
    users_col = sessions_col = None

# === IN-MEMORY FALLBACK ===
user_states = defaultdict(lambda: {
    "points": 50,
    "banned": False,
    "referrals": 0,
    "last_refill": None,
    "last_answer": None,
    "awaiting": False
})
user_stats = defaultdict(int)  # questions asked
total_stats = 0
total_users_set = set()
START_TIME = datetime.now(timezone.utc)

# === KEYBOARDS ===
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ask a Question", callback_data="ask_btn")],
        [InlineKeyboardButton("Help", callback_data="help_btn"), InlineKeyboardButton("Stats", callback_data="stats_btn")]
    ])

def followup_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Explain More", callback_data="follow_explain"),
         InlineKeyboardButton("Summarize", callback_data="follow_summarize")],
        [InlineKeyboardButton("Code Example", callback_data="follow_code")],
        [InlineKeyboardButton("« Back", callback_data="ask_btn")]
    ])

# === GEMINI QUERY ===
async def query_gemini(prompt: str) -> str:
    if not gemini_model:
        return "Gemini API key not configured."
    
    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content, prompt, generation_config={"temperature": 0.7}
        )
        return response.text or "No response from Gemini."
    except Exception as e:
        return f"Gemini Error: {str(e)}"

# === USER MANAGEMENT ===
async def get_user_doc(uid: int):
    if users_col:
        return await users_col.find_one({"user_id": uid})
    return user_states[uid]

async def ensure_user(uid: int, ref_param: str | None = None):
    total_users_set.add(uid)
    
    if users_col:
        user = await users_col.find_one({"user_id": uid})
        if not user:
            doc = {
                "user_id": uid,
                "points": 50,
                "banned": False,
                "referrals": 0,
                "last_refill": datetime.now(timezone.utc).isoformat(),
                "joined_at": datetime.now(timezone.utc).isoformat(),
                "referred_by": None
            }
            if ref_param and ref_param.startswith("ref"):
                try:
                    ref_id = int(ref_param[3:])
                    if ref_id != uid:
                        await users_col.update_one(
                            {"user_id": ref_id},
                            {"$inc": {"points": 5, "referrals": 1}}
                        )
                        doc["referred_by"] = ref_id
                        try:
                            await app.send_message(ref_id, "🎉 You earned 5 points! Someone joined via your link!")
                        except:
                            pass
                except:
                    pass
            await users_col.insert_one(doc)
            return doc
        return user
    else:
        user_states[uid]  # init
        return user_states[uid]

async def get_points(uid: int) -> int:
    user = await get_user_doc(uid)
    return user.get("points", 50) if user else 50

async def deduct_points(uid: int, amount: int = 1):
    if users_col:
        await users_col.update_one({"user_id": uid}, {"$inc": {"points": -amount}})
    else:
        user_states[uid]["points"] = max(0, user_states[uid]["points"] - amount)

async def daily_refill(uid: int):
    today = date.today().isoformat()
    user = await get_user_doc(uid)
    
    last_refill = user.get("last_refill", "").split("T")[0] if user.get("last_refill") else None
    
    if last_refill != today:
        if users_col:
            await users_col.update_one(
                {"user_id": uid},
                {"$inc": {"points": 20}, "$set": {"last_refill": datetime.now(timezone.utc).isoformat()}}
            )
        else:
            user_states[uid]["points"] += 20
            user_states[uid]["last_refill"] = today

async def can_ask(uid: int):
    await ensure_user(uid)
    await daily_refill(uid)
    
    user = await get_user_doc(uid)
    if user.get("banned"):
        reason = user.get("ban_reason", "No reason given.")
        return False, f"You are banned. Reason: {reason}"
    
    points = await get_points(uid)
    if points <= 0:
        return False, "No points left! Come back tomorrow for +20 bonus points."
    
    return True, None

def format_header(q: str) -> str:
    short = textwrap.shorten(q, width=300, placeholder="...")
    return f"<b>Question:</b>\n<blockquote expandable>{short}</blockquote>\n\n<b>Answer:</b>\n"

def is_admin(uid: int) -> bool:
    return uid == OWNER_ID or uid in ADMIN_IDS

# Commands to set bot commands and notify owner
def reset_and_set_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    # Reset
    requests.post(url, json={"commands": []})
    # Set new
    commands = [
        {"command": "start", "description": "✅ ᴄʜᴇᴄᴋ ɪꜰ ᴛʜᴇ ʙᴏᴛ ɪꜱ ᴀʟɪᴠᴇ"},
        {"command": "stop", "description": "⏹️ ᴛᴇʀᴍɪɴᴀᴛᴇ ᴛʜᴇ ᴏɴɢᴏɪɴɢ ᴘʀᴏᴄᴇꜱꜱ"},
        {"command": "reset", "description": "♻️ ʀᴇꜱᴇᴛ ᴛʜᴇ ʙᴏᴛ"},
        {"command": "restart", "description": "♻️ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ"},
        {"command": "logs", "description": "👁 ᴠɪᴇᴡ ʙᴏᴛ ᴀᴄᴛɪᴠɪᴛʏ"},
        {"command": "myplan", "description": "⏸️ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ ᴘʟᴀɴ"},
        {"command": "refer", "description": "🔗 ɢᴇᴛ ʏᴏᴜʀ ʀᴇꜰᴇʀʀᴀʟ ʟɪɴᴋ"},
        {"command": "balance", "description": "💳 ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴘᴏɪɴᴛs"},
        {"command": "ask", "description": "💬 ᴀsᴋ ɢᴇᴍɪɴɪ ᴀɪ ᴀ ǫᴜᴇsᴛɪᴏɴ"},
    ]

    requests.post(url, json={"commands": commands})

# === STARTUP ===
def startup_tasks():
    if OWNER_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{AI_BOT_TOKEN}/sendMessage",
                data={"chat_id": OWNER_ID, "text": "Gemini AI Bot is now LIVE!"},
                timeout=10
            )
        except:
            pass
    try:
        reset_and_set_commands()
    except Exception:
        pass
# === COMMANDS ===
@app.on_message(filters.command("start"))
async def start(_, msg):
    ref = msg.command[1] if len(msg.command) > 1 else None
    await ensure_user(msg.from_user.id, ref)
    
    await msg.reply_text(
        "<b>🤖 Welcome to Gemini AI Bot!</b>\n\n"
        "Ask anything using:\n"
        "• <code>/ask What is quantum physics?</code>\n"
        "• Reply to any message with /ask\n"
        "• Just type /ask → I’ll wait for your question\n\n"
        "<i>Powered by Google Gemini • Smart & Fast</i>",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    await msg.reply_text(
        "<b>🆘 Help & Commands</b>\n\n"
        "/ask - Ask a question\n"
        "/balance - Check your points\n"
        "/refer - Get your referral link (+5 pts each)\n"
        "/stats - Bot statistics\n"
        "/restart - Cancel waiting input\n\n"
        "<i>New users get 50 points • +20 daily bonus</i>",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("balance"))
async def balance(_, msg):
    points = await get_points(msg.from_user.id)
    await msg.reply_text(f"<b>💰 Your Points:</b> {points}", parse_mode=ParseMode.HTML)

@app.on_message(filters.command("refer"))
async def refer(_, msg):
    uid = msg.from_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}" if BOT_USERNAME else f"https://t.me/{msg.chat.username}?start=ref{uid}"
    
    user = await get_user_doc(uid)
    refs = user.get("referrals", 0) if user else 0
    
    await msg.reply_text(
        "<b>📩 Invite Friends & Earn!</b>\n\n"
        f"Your Link: <code>{link}</code>\n\n"
        f"Referrals: <b>{refs}</b>\n"
        f"Reward: <b>+5 points</b> per user!",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("stats"))
async def stats(_, msg):
    uptime = datetime.now(timezone.utc) - START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    
    total_users = len(total_users_set)
    if users_col:
        total_users = await users_col.estimated_document_count()
    
    await msg.reply_text(
        f"<b>📊 Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"❓ Total Questions: <b>{total_stats}</b>\n"
        f"🟢 Uptime: <b>{h}h {m}m {s}s</b>\n"
        f"💬 Your Asks: <b>{user_stats[msg.from_user.id]}</b>",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("restart"))
async def restart(_, msg):
    user_states[msg.from_user.id]["awaiting"] = False
    await msg.reply_text("✅ Session cleared. Use /ask to start again.")

# === MAIN ASK COMMAND ===
@app.on_message(filters.command("ask") & filters.private)
async def ask_command(client: Client, message):
    global total_stats
    uid = message.from_user.id
    await ensure_user(uid)
    
    ok, reason = await can_ask(uid)
    if not ok:
        return await message.reply_text(f"❌ {reason}", parse_mode=ParseMode.HTML)
    
    question = None
    
    # Case 1: Question in command
    if len(message.command) > 1:
        question = " ".join(message.command[1:])
    
    # Case 2: Replied message
    elif message.reply_to_message:
        if message.reply_to_message.text:
            question = message.reply_to_message.text
        elif message.reply_to_message.caption:
            question = message.reply_to_message.caption
        else:
            question = "Describe this image/document."
    
    # Case 3: Wait for input
    if not question:
        user_states[uid]["awaiting"] = True
        await message.reply_text(
            "✍️ <b>Send your question below:</b>\n\n"
            "<i>Send /quit to cancel • Timeout: 2 minutes</i>",
            parse_mode=ParseMode.HTML
        )
        try:
            response = await client.listen(chat_id=message.chat.id, user_id=uid, timeout=120)
            if response.text and response.text.strip().lower() == "/quit":
                user_states[uid]["awaiting"] = False
                return await message.reply_text("❌ Cancelled.")
            question = response.text.strip()
        except asyncio.TimeoutError:
            user_states[uid]["awaiting"] = False
            return await message.reply_text("⏰ Timed out. Use /ask again.")
        except:
            user_states[uid]["awaiting"] = False
            return await message.reply_text("⚠️ Error. Try again.")
    
    if not question or len(question.strip()) < 2:
        return await message.reply_text("❌ Please provide a valid question.")
    
    # Deduct point & process
    await deduct_points(uid)
    user_states[uid]["awaiting"] = False
    user_stats[uid] += 1
    total_stats += 1
    
    thinking = await message.reply_text("🧠 <i>Thinking with Gemini...</i>", parse_mode=ParseMode.HTML)
    
    answer = await query_gemini(question)
    user_states[uid]["last_answer"] = {"question": question, "answer": answer}
    
    if sessions_col:
        await sessions_col.update_one(
            {"user_id": uid},
            {"$set": {"last_answer": user_states[uid]["last_answer"]}},
            upsert=True
        )
    
    header = format_header(question)
    chunks = [answer[i:i+3800] for i in range(0, len(answer), 3800)]
    
    await thinking.edit_text(header + chunks[0], reply_markup=followup_keyboard(), parse_mode=ParseMode.HTML)
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)

# === CALLBACK QUERIES ===
@app.on_callback_query()
async def callbacks(_, cq):
    uid = cq.from_user.id
    data = cq.data
    
    if data == "ask_btn":
        await cq.message.reply_text("Send your question now!")
        await cq.answer()
    
    elif data == "help_btn":
        await cq.message.reply_text("Use /help for commands.")
        await cq.answer()
    
    elif data == "stats_btn":
        await cq.message.reply_text("Use /stats for bot info.")
        await cq.answer()
    
    elif data.startswith("follow_"):
        last = user_states[uid].get("last_answer")
        if not last:
            return await cq.answer("No previous question found.", show_alert=True)
        
        action = data.split("_")[1]
        q = last["question"]
        prev = last["answer"]
        
        prompts = {
            "explain": f"Explain in more detail about: {q}\nPrevious answer: {prev}",
            "summarize": f"Summarize this in 3-5 bullet points:\n{prev}",
            "code": f"Provide a clean, working Python code example for: {q}\nContext: {prev}"
        }
        
        prompt = prompts.get(action, "")
        if not prompt:
            return await cq.answer("Invalid action.")
        
        await cq.message.reply_text("<i>Generating...</i>", parse_mode=ParseMode.HTML)
        await cq.answer()
        
        follow = await query_gemini(prompt)
        user_states[uid]["last_answer"]["answer"] = follow
        
        title = {"explain": "Detailed Explanation", "summarize": "Summary", "code": "Code Example"}
        header = f"<b>{title.get(action, action.title())}:</b>\n<blockquote>{textwrap.shorten(q, 100)}</blockquote>\n\n"
        
        chunks = [follow[i:i+3800] for i in range(0, len(follow), 3800)]
        await cq.message.reply_text(header + chunks[0], parse_mode=ParseMode.HTML)
        for c in chunks[1:]:
            await cq.message.reply_text(c, parse_mode=ParseMode.HTML)

# === ADMIN COMMANDS ===
@app.on_message(filters.command(["add_points", "remove_points", "ban", "unban"]) & filters.private)
async def admin_commands(_, msg):
    if not is_admin(msg.from_user.id):
        return await msg.reply("🔒 Admin only.")
    
    cmd = msg.command[0]
    
    if cmd == "add_points" or cmd == "remove_points":
        if len(msg.command) < 3:
            return await msg.reply(f"Usage: /{cmd} <user_id> <amount>")
        try:
            target = int(msg.command[1])
            amount = int(msg.command[2])
            delta = amount if cmd == "add_points" else -abs(amount)
            if users_col:
                result = await users_col.update_one(
                    {"user_id": target},
                    {"$inc": {"points": delta}},
                    upsert=True
                )
                new_points = await get_points(target)
            else:
                user_states[target]["points"] = max(0, user_states[target].get("points", 50) + delta)
                new_points = user_states[target]["points"]
            await msg.reply(f"{'Added' if delta > 0 else 'Removed'} {abs(delta)} points → User {target}\nNew balance: {new_points}")
        except:
            await msg.reply("Invalid user ID or amount.")
    
    elif cmd == "ban":
        if len(msg.command) < 2:
            return await msg.reply("/ban <user_id> [reason]")
        target = int(msg.command[1])
        reason = " ".join(msg.command[2:]) or "No reason"
        if users_col:
            await users_col.update_one({"user_id": target}, {"$set": {"banned": True, "ban_reason": reason}}, upsert=True)
        else:
            user_states[target]["banned"] = True
            user_states[target]["ban_reason"] = reason
        await msg.reply(f"🚫 Banned user {target}\nReason: {reason}")
    
    elif cmd == "unban":
        if len(msg.command) < 2:
            return await msg.reply("/unban <user_id>")
        target = int(msg.command[1])
        if users_col:
            await users_col.update_one({"user_id": target}, {"$set": {"banned": False, "ban_reason": None}})
        else:
            user_states[target]["banned"] = False
        await msg.reply(f"✅ Unbanned user {target}")

# === START BOT ===
print("Gemini AI Bot Starting...")
startup_tasks()
app.run()
