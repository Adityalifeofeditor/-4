"""
Gemini AI Telegram Bot (full working example)

- Single-file bot you can run as `python bot.py`
- Uses Pyrogram Client and optionally motor (async MongoDB)
- Uses google.genai if GEMINI_API_KEY provided
- /admin_settings command shows an admin panel with buttons for:
  Add Points, Remove Points, Ban, Unban, All Ban List, API Key (view/set)
- Works with MongoDB when MONGO_URI + motor installed; otherwise uses in-memory dicts
- Good comments throughout for easy understanding
"""

import os
import textwrap
import asyncio
from collections import defaultdict
from datetime import datetime, timezone, date
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

# Optional: motor (async MongoDB)
try:
    import motor.motor_asyncio as motor
    mongo_available = True
except ImportError:
    mongo_available = False

# Optional: google genai for Gemini (if you want to use official lib)
try:
    from google import genai
    genai_available = True
except Exception:
    genai_available = False

load_dotenv()

# === ENV VARIABLES ===
AI_BOT_TOKEN = os.getenv("AI_BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # optional
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip("@")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Basic validation
if not all([AI_BOT_TOKEN, API_ID, API_HASH]):
    raise RuntimeError("Missing required .env variables: AI_BOT_TOKEN, API_ID, API_HASH")

API_ID = int(API_ID)
app = Client("gemini_ask_bot", bot_token=AI_BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# === GLOBALS & FALLBACKS ===
# Mongo collections (if available)
if MONGO_URI and mongo_available:
    mongo_client = motor.AsyncIOMotorClient(MONGO_URI)
    db = mongo_client["gemini_bot"]
    users_col = db.users
    sessions_col = db.sessions
    settings_col = db.settings  # store bot settings like gemini_api_key
else:
    users_col = sessions_col = settings_col = None

# In-memory fallback state
user_states = defaultdict(lambda: {
    "points": 50,
    "banned": False,
    "referrals": 0,
    "last_refill": None,
    "last_answer": None,
    "awaiting": False
})
user_stats = defaultdict(int)  # questions asked per user
total_stats = 0
total_users_set = set()
START_TIME = datetime.now(timezone.utc)

# Bot runtime settings (persisted in DB if available)
bot_settings = {
    "gemini_api_key": GEMINI_API_KEY or None
}

# If genai is available and key provided, initialize client
gemini_model = None
if genai_available and bot_settings["gemini_api_key"]:
    try:
        genai.configure(api_key=bot_settings["gemini_api_key"])
        gemini_model = genai
    except Exception:
        gemini_model = None

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

# Admin panel keyboard (shown by /admin_settings)
def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Points", callback_data="admin_add_points"),
            InlineKeyboardButton("➖ Remove Points", callback_data="admin_remove_points")
        ],
        [
            InlineKeyboardButton("🚫 Ban", callback_data="admin_ban"),
            InlineKeyboardButton("✅ Unban", callback_data="admin_unban")
        ],
        [
            InlineKeyboardButton("📋 All Ban List", callback_data="admin_ban_list"),
            InlineKeyboardButton("🔑 API Key", callback_data="admin_api_key")
        ],
        [InlineKeyboardButton("Close", callback_data="admin_close")]
    ])

# === UTILITIES: DB / in-memory abstraction ===

async def get_user_doc(uid: int):
    """Return user document from DB or in-memory state."""
    if users_col:
        user = await users_col.find_one({"user_id": uid})
        return user
    return user_states[uid]

async def ensure_user(uid: int, ref_param: str | None = None):
    """Ensure user exists in DB or in-memory. Reward referral if present."""
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
            # handle referral param like "ref12345"
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
        # triggers creation via defaultdict when accessing user_states[uid]
        user_states[uid]
        return user_states[uid]

async def get_points(uid: int) -> int:
    user = await get_user_doc(uid)
    return user.get("points", 50) if user else 50

async def deduct_points(uid: int, amount: int = 1):
    if users_col:
        await users_col.update_one({"user_id": uid}, {"$inc": {"points": -amount}})
    else:
        user_states[uid]["points"] = max(0, user_states[uid]["points"] - amount)

async def add_points(uid: int, amount: int):
    if users_col:
        await users_col.update_one({"user_id": uid}, {"$inc": {"points": amount}}, upsert=True)
    else:
        user_states[uid]["points"] = user_states[uid].get("points", 50) + amount

async def set_ban(uid: int, banned: bool, reason: str | None = None):
    if users_col:
        await users_col.update_one({"user_id": uid}, {"$set": {"banned": banned, "ban_reason": reason}}, upsert=True)
    else:
        user_states[uid]["banned"] = banned
        user_states[uid]["ban_reason"] = reason

async def list_all_banned():
    if users_col:
        cursor = users_col.find({"banned": True}, {"user_id": 1, "ban_reason": 1})
        rows = []
        async for d in cursor:
            rows.append((d.get("user_id"), d.get("ban_reason", "No reason")))
        return rows
    else:
        return [(uid, s.get("ban_reason", "No reason")) for uid, s in user_states.items() if s.get("banned")]

# daily refill logic (keeps track of last_refill)
async def daily_refill(uid: int):
    today = date.today().isoformat()
    user = await get_user_doc(uid)
    last_refill = None
    if user:
        last_refill = user.get("last_refill", "")
        last_refill = last_refill.split("T")[0] if last_refill else None
    if last_refill != today:
        if users_col:
            await users_col.update_one(
                {"user_id": uid},
                {"$inc": {"points": 20}, "$set": {"last_refill": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )
        else:
            user_states[uid]["points"] += 20
            user_states[uid]["last_refill"] = today

async def can_ask(uid: int):
    """Check if user can ask: not banned and has points (after refill)."""
    await ensure_user(uid)
    await daily_refill(uid)
    user = await get_user_doc(uid)
    if user and user.get("banned"):
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

# === Gemini Query Wrapper ===
async def query_gemini(prompt: str) -> str:
    """
    Query Gemini model (genai). If unavailable returns an explanatory string.
    This runs the blocking call inside a thread to avoid blocking the event loop.
    """
    if not gemini_model:
        # No model configured: return helpful message
        return "Gemini API not configured. Admins can set it via /admin_settings → API Key."
    try:
        # If using google.genai, call model.generate_content in a thread
        def _call():
            # adapt to genai usage - may vary depending on your genai version
            res = gemini_model.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return getattr(res, "text", None) or str(res)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call)
    except Exception as e:
        return f"Gemini Error: {e}"

# === Bot command helpers ===
async def reset_and_set_commands():
    """Optional: set telegram bot commands using the HTTP API (simple)."""
    url = f"https://api.telegram.org/bot{AI_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "start", "description": "✅ Check if bot is alive"},
        {"command": "ask", "description": "💬 Ask Gemini something"},
        {"command": "balance", "description": "💳 Check your points"},
        {"command": "refer", "description": "🔗 Get referral link"},
        {"command": "stats", "description": "📊 Bot statistics"},
        {"command": "restart", "description": "♻️ Cancel waiting input"},
        {"command": "admin_settings", "description": "🔒 Admin panel (admins only)"}
    ]
    try:
        import requests
        requests.post(url, json={"commands": commands}, timeout=10)
    except Exception:
        pass

def startup_tasks():
    """Run on startup: notify owner and set commands."""
    if OWNER_ID:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{AI_BOT_TOKEN}/sendMessage",
                data={"chat_id": OWNER_ID, "text": "Gemini AI Bot is now LIVE!"},
                timeout=10
            )
        except Exception:
            pass
    try:
        asyncio.get_event_loop().create_task(async_reset_commands())
    except Exception:
        # fallback sync
        try:
            reset_and_set_commands()
        except:
            pass

async def async_reset_commands():
    # run set commands in thread to avoid blocking
    await asyncio.get_event_loop().run_in_executor(None, reset_and_set_commands)

# === COMMAND HANDLERS ===

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
        "/restart - Cancel waiting input\n"
        "/admin_settings - Admin panel (admins only)\n\n"
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
    global total_stats
    uptime = datetime.now(timezone.utc) - START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    total_users = len(total_users_set)
    if users_col:
        try:
            total_users = await users_col.estimated_document_count()
        except:
            total_users = len(total_users_set)
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

# === MAIN ASK COMMAND (private only) ===
@app.on_message(filters.command("ask") & filters.private)
async def ask_command(client: Client, message):
    global total_stats, gemini_model
    uid = message.from_user.id
    await ensure_user(uid)

    ok, reason = await can_ask(uid)
    if not ok:
        return await message.reply_text(f"❌ {reason}", parse_mode=ParseMode.HTML)

    question = None
    # Command text
    if len(message.command) > 1:
        question = " ".join(message.command[1:])
    # Reply to message
    elif message.reply_to_message:
        if message.reply_to_message.text:
            question = message.reply_to_message.text
        elif message.reply_to_message.caption:
            question = message.reply_to_message.caption
        else:
            question = "Describe this image/document."
    # Ask interactively
    if not question:
        sos = await app.ask(
            message.chat.id,
            text=(
                "📥 Send me your question — I'll answer it.\n\n"
                "Send `/quit` to cancel."
            ),
        )
        user_input = (sos.text or "").strip()
        if user_input.lower() == "/quit":
            return await message.reply_text("<b><i>❌ The process has been cancelled.</i></b>", parse_mode=ParseMode.HTML)
        question = user_input

    if not question or len(question.strip()) < 2:
        return await message.reply_text("❌ Please provide a valid question.")

    # Deduct a point and track usage
    await deduct_points(uid)
    user_states[uid]["awaiting"] = False
    user_stats[uid] += 1
    total_stats += 1

    thinking = await message.reply_text("🧠 <i>Thinking with Gemini...</i>", parse_mode=ParseMode.HTML)

    # Query Gemini
    answer = await query_gemini(question)
    # Save last answer in state & DB
    user_states[uid]["last_answer"] = {"question": question, "answer": answer}
    if sessions_col:
        await sessions_col.update_one({"user_id": uid}, {"$set": {"last_answer": user_states[uid]["last_answer"]}}, upsert=True)

    header = format_header(question)
    # chunk if long (Telegram message length guard)
    chunks = [answer[i:i+3800] for i in range(0, len(answer), 3800)]
    await thinking.edit_text(header + (chunks[0] if chunks else "No answer."), reply_markup=followup_keyboard(), parse_mode=ParseMode.HTML)
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)

# === CALLBACK QUERIES (followups + admin actions) ===
@app.on_callback_query()
async def callbacks(_, cq):
    uid = cq.from_user.id
    data = cq.data or ""

    # User-facing followups
    if data == "ask_btn":
        await cq.message.reply_text("Send your question now!")
        await cq.answer()
        return

    if data == "help_btn":
        await cq.message.reply_text("Use /help for commands.")
        await cq.answer()
        return

    if data == "stats_btn":
        await cq.message.reply_text("Use /stats for bot info.")
        await cq.answer()
        return

    if data.startswith("follow_"):
        # follow-up actions: explain, summarize, code
        last = user_states[uid].get("last_answer")
        if not last:
            return await cq.answer("No previous question found.", show_alert=True)
        action = data.split("_", 1)[1]
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
        await cq.message.reply_text(header + (chunks[0] if chunks else "No content."), parse_mode=ParseMode.HTML)
        for c in chunks[1:]:
            await cq.message.reply_text(c, parse_mode=ParseMode.HTML)
        return

    # --- ADMIN PANEL ACTIONS ---
    if data.startswith("admin_"):
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return

        action = data.split("_", 1)[1]

        # Close admin panel
        if action == "close":
            try:
                await cq.message.edit_text("Admin panel closed.")
            except:
                pass
            await cq.answer()
            return

        # Add points (interactive)
        if action == "add_points":
            await cq.answer()
            prompt = await app.ask(uid, text="Send target user_id and amount to add (format: <user_id> <amount>)\nSend /cancel to stop.")
            txt = (prompt.text or "").strip()
            if txt.lower() == "/cancel":
                return await app.send_message(uid, "Cancelled.")
            try:
                parts = txt.split()
                target = int(parts[0]); amount = int(parts[1])
                await add_points(target, amount)
                new_points = await get_points(target)
                await app.send_message(uid, f"✅ Added {amount} points to {target}. New balance: {new_points}")
            except Exception as e:
                await app.send_message(uid, f"❌ Error parsing input or updating points: {e}")
            return

        # Remove points (interactive)
        if action == "remove_points":
            await cq.answer()
            prompt = await app.ask(uid, text="Send target user_id and amount to remove (format: <user_id> <amount>)\nSend /cancel to stop.")
            txt = (prompt.text or "").strip()
            if txt.lower() == "/cancel":
                return await app.send_message(uid, "Cancelled.")
            try:
                parts = txt.split()
                target = int(parts[0]); amount = int(parts[1])
                await add_points(target, -abs(amount))
                new_points = await get_points(target)
                await app.send_message(uid, f"✅ Removed {amount} points from {target}. New balance: {new_points}")
            except Exception as e:
                await app.send_message(uid, f"❌ Error parsing input or updating points: {e}")
            return

        # Ban user (interactive)
        if action == "ban":
            await cq.answer()
            prompt = await app.ask(uid, text="Send target user_id and optional reason (format: <user_id> [reason])\nSend /cancel to stop.")
            txt = (prompt.text or "").strip()
            if txt.lower() == "/cancel":
                return await app.send_message(uid, "Cancelled.")
            try:
                parts = txt.split(None, 1)
                target = int(parts[0]); reason = parts[1] if len(parts) > 1 else "No reason"
                await set_ban(target, True, reason)
                await app.send_message(uid, f"🚫 Banned user {target}\nReason: {reason}")
            except Exception as e:
                await app.send_message(uid, f"❌ Error parsing input or banning: {e}")
            return

        # Unban user (interactive)
        if action == "unban":
            await cq.answer()
            prompt = await app.ask(uid, text="Send target user_id to unban (format: <user_id>)\nSend /cancel to stop.")
            txt = (prompt.text or "").strip()
            if txt.lower() == "/cancel":
                return await app.send_message(uid, "Cancelled.")
            try:
                target = int(txt.split()[0])
                await set_ban(target, False, None)
                await app.send_message(uid, f"✅ Unbanned user {target}")
            except Exception as e:
                await app.send_message(uid, f"❌ Error parsing input or unbanning: {e}")
            return

        # All ban list: show small list (paginated would be better for many users)
        if action == "ban_list":
            await cq.answer()
            rows = await list_all_banned()
            if not rows:
                return await app.send_message(uid, "No banned users found.")
            text = "🚫 <b>Banned Users</b>\n\n"
            for (user_id, reason) in rows[:50]:  # limit to 50 to avoid huge message
                text += f"• <code>{user_id}</code> — {reason}\n"
            if len(rows) > 50:
                text += f"\n...and {len(rows)-50} more."
            await app.send_message(uid, text, parse_mode=ParseMode.HTML)
            return

        # API Key management
        if action == "api_key":
            await cq.answer()
            # Show current key masked (if any) and let admin set new one
            key_stored = None
            if settings_col:
                existing = await settings_col.find_one({"name": "gemini_api_key"})
                key_stored = existing.get("value") if existing else None
            else:
                key_stored = bot_settings.get("gemini_api_key")

            masked = (key_stored[:4] + "..." + key_stored[-4:]) if key_stored else "<i>Not set</i>"
            await app.send_message(uid, f"Current Gemini API Key: {masked}", parse_mode=ParseMode.HTML)
            prompt = await app.ask(uid, text="Send new GEMINI API KEY to set, or send /view to only view, /clear to remove it. Send /cancel to stop.")
            txt = (prompt.text or "").strip()
            if txt.lower() == "/cancel":
                return await app.send_message(uid, "Cancelled.")
            if txt.lower() == "/view":
                return await app.send_message(uid, f"Current (masked): {masked}", parse_mode=ParseMode.HTML)
            if txt.lower() == "/clear":
                # clear key
                if settings_col:
                    await settings_col.delete_one({"name": "gemini_api_key"})
                bot_settings["gemini_api_key"] = None
                # also unset genai client
                global gemini_model
                gemini_model = None
                return await app.send_message(uid, "✅ Gemini API key cleared.")
            # else set new key
            new_key = txt
            if settings_col:
                await settings_col.update_one({"name": "gemini_api_key"}, {"$set": {"value": new_key}}, upsert=True)
            bot_settings["gemini_api_key"] = new_key
            # attempt to configure genai client if library present
            if genai_available:
                try:
                    genai.configure(api_key=new_key)
                    gemini_model = genai
                    await app.send_message(uid, "✅ Gemini API key set and genai initialized.")
                except Exception as e:
                    gemini_model = None
                    await app.send_message(uid, f"⚠️ Key saved but genai init failed: {e}")
            else:
                gemini_model = None
                await app.send_message(uid, "✅ Gemini API key saved to settings (genai library not installed).")
            return

    # If we reach here: unknown callback
    await cq.answer()

# === ADMIN SETTINGS COMMAND (shows panel) ===
@app.on_message(filters.command("admin_settings") & filters.private)
async def admin_settings_cmd(_, msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return await msg.reply_text("🔒 You are not authorized to use this command.")
    await msg.reply_text("<b>🔧 Admin Settings</b>\nChoose an action:", reply_markup=admin_panel_keyboard(), parse_mode=ParseMode.HTML)

# === Start the bot ===
if __name__ == "__main__":
    print("Gemini AI Bot Starting...")
    startup_tasks()
    app.run()
