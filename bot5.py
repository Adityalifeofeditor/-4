import os
import textwrap
import asyncio
from collections import defaultdict
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai

# --- Mongo async driver ---
try:
    import motor.motor_asyncio as motor
except ImportError:
    motor = None

# Load env variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
OWNER_ID = os.getenv("OWNER_ID")  # REQUIRED: owner's Telegram user id (single integer)
MONGO_URI = os.getenv("MONGO_URI")  # REQUIRED: mongodb connection string (e.g. mongodb://user:pass@host:port/db)
ENV_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # optional initial key

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("BOT_TOKEN, API_ID, and API_HASH must be defined in .env")

if not OWNER_ID:
    raise RuntimeError("OWNER_ID must be set in .env (owner Telegram user id)")

if motor is None:
    raise RuntimeError("motor (async MongoDB driver) is required. Install with: pip install motor")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI must be defined in .env (mongodb connection string)")

OWNER_ID = int(OWNER_ID)

# Initialize bot
app = Client(
    name="ask_bot",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH,
)

# Globals for DB & Gemini
db_client = None
db = None
gemini_client = None

# Simple in-memory state (still persisted to DB for counters)
user_states = defaultdict(lambda: {"awaiting": False})
# keep a short in-memory cache for user stats to avoid frequent DB hits
user_stats_cache = defaultdict(int)

# ---------- Helper: DB init & access ----------
async def init_db():
    global db_client, db, gemini_client
    db_client = motor.AsyncIOMotorClient(MONGO_URI)
    # default database derived from URI or 'askbot'
    dbname = "askbot"
    db = db_client[dbname]
    # Ensure indexes
    await db.users.create_index("points")
    await db.users.create_index("stats")
    await db.settings.create_index("key", unique=True)

    # Load stored gemini key (if set), otherwise use env
    setting = await db.settings.find_one({"key": "gemini_api_key"})
    api_key = setting["value"] if setting and "value" in setting else ENV_GEMINI_API_KEY
    if api_key:
        set_gemini_client(api_key)
    else:
        gemini_client = None


def set_gemini_client(api_key: str):
    """(re)initialize global gemini client"""
    global gemini_client
    gemini_client = genai.Client(api_key=api_key)


# ---------- DB helpers ----------
async def get_setting(key: str):
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc else None


async def set_setting(key: str, value):
    await db.settings.update_one({"key": key}, {"$set": {"key": key, "value": value}}, upsert=True)


async def get_user(uid: int):
    doc = await db.users.find_one({"_id": uid})
    if doc:
        return doc
    # create default
    new = {"_id": uid, "points": 0, "stats": 0, "banned": False}
    await db.users.insert_one(new)
    return new


async def inc_user_stats(uid: int, amount: int = 1):
    await db.users.update_one({"_id": uid}, {"$inc": {"stats": amount}}, upsert=True)
    user_stats_cache[uid] += amount
    # update total
    await db.settings.update_one({"key": "total_questions"}, {"$inc": {"value": amount}}, upsert=True)


async def add_points_to_user(uid: int, amount: int):
    await db.users.update_one({"_id": uid}, {"$inc": {"points": amount}}, upsert=True)


async def set_ban(uid: int, banned: bool):
    await db.users.update_one({"_id": uid}, {"$set": {"banned": banned}}, upsert=True)


async def list_banned_users(limit=200):
    cursor = db.users.find({"banned": True}).limit(limit)
    return [doc["_id"] async for doc in cursor]


async def get_total_questions():
    setting = await db.settings.find_one({"key": "total_questions"})
    return setting["value"] if setting and "value" in setting else 0


# ---------- Gemini query ----------
def chunk_text(text, size=3800):
    for i in range(0, len(text), size):
        yield text[i:i+size]


async def query_gemini(prompt: str):
    if gemini_client is None:
        raise RuntimeError("Gemini API key not set. Owner must run /set_api to set it.")
    loop = asyncio.get_event_loop()

    def _call():
        # synchronous call executed in executor to avoid blocking
        res = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return getattr(res, "text", None) or str(res)

    return await loop.run_in_executor(None, _call)


# ---------- Keyboards ----------
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

# ---------- Bot handlers ----------
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "👋 *Welcome to Gemini AI Ask Bot*\n\n"
        "You can ask questions in 3 ways:\n"
        "• `/ask your question`\n"
        "• Reply to any message with `/ask`\n"
        "• Send `/ask` alone and I will wait for your question\n\n"
        "Use /help for full info.",
        reply_markup=main_keyboard()
    )


@app.on_message(filters.command("help"))
async def help_cmd(_, msg):
    await msg.reply(
        "*Commands*\n\n"
        "• `/ask <question>` — Ask instantly\n"
        "• Reply to a message with `/ask`\n"
        "• `/ask` alone — I wait for your next message\n"
        "• `/restart` — Clear pending request\n"
        "• `/stats` — Show your usage statistics\n\n"
        "Owner-only commands:\n"
        "• `/set_api <api_key>` — set Gemini API key\n"
        "• `/add_points <user_id> <amount>` or reply + `/add_points <amount>`\n"
        "• `/rem_points <user_id> <amount>` or reply + `/rem_points <amount>`\n"
        "• `/ban <user_id>` or reply + `/ban`\n"
        "• `/unban <user_id>` or reply + `/unban`\n"
        "• `/banlist` — list banned users (owner only)\n"
    )


@app.on_message(filters.command("restart"))
async def restart(_, msg):
    user_states[msg.from_user.id]["awaiting"] = False
    await msg.reply("🔄 Session restarted. Use /ask to ask a question again.")


@app.on_message(filters.command("stats"))
async def stats(_, msg):
    uid = msg.from_user.id
    # if owner asks " /stats all " return global
    if uid == OWNER_ID and len(msg.command) > 1 and msg.command[1].lower() in ("all", "global"):
        total = await get_total_questions()
        await msg.reply(f"*Global Stats:*\nTotal Questions: {total}")
        return

    user_doc = await get_user(uid)
    total = await get_total_questions()
    await msg.reply(
        f"*Your Stats:*\nQuestions asked: {user_doc.get('stats', 0)}\nPoints: {user_doc.get('points', 0)}\n\n"
        f"*Total Questions (all users):* {total}"
    )


# ---------- Admin: set_api ----------
@app.on_message(filters.command("set_api"))
async def set_api(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    # Accept inline: /set_api key...
    if len(msg.command) > 1:
        key = " ".join(msg.command[1:]).strip()
    else:
        # or ask interactive
        asked = await app.ask(msg.chat.id, text="Send the Gemini API key (or /cancel):")
        key = (asked.text or "").strip()

    if not key:
        return await msg.reply("No key provided. Operation cancelled.")

    # store in DB and apply
    await set_setting("gemini_api_key", key)
    set_gemini_client(key)
    await msg.reply("✅ Gemini API key saved and applied.")


# ---------- Admin: add_points / rem_points ----------
def _parse_target_and_amount(msg):
    """
    returns (target_uid, amount) or (None, None) on failure
    usage:
      /add_points <user_id> <amount>
      or reply to a user's message and use /add_points <amount>
    """
    if len(msg.command) >= 3:
        try:
            target = int(msg.command[1])
            amount = int(msg.command[2])
            return target, amount
        except Exception:
            return None, None
    elif len(msg.command) == 2 and msg.reply_to_message:
        try:
            target = msg.reply_to_message.from_user.id
            amount = int(msg.command[1])
            return target, amount
        except Exception:
            return None, None
    else:
        return None, None


@app.on_message(filters.command("add_points"))
async def add_points(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    target, amount = _parse_target_and_amount(msg)
    if target is None or amount is None:
        return await msg.reply("Usage:\n`/add_points <user_id> <amount>`\nor reply to a user and send `/add_points <amount>`")
    await add_points_to_user(target, amount)
    await msg.reply(f"✅ Added {amount} points to user `{target}`.")


@app.on_message(filters.command("rem_points"))
async def rem_points(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    target, amount = _parse_target_and_amount(msg)
    if target is None or amount is None:
        return await msg.reply("Usage:\n`/rem_points <user_id> <amount>`\nor reply to a user and send `/rem_points <amount>`")
    await add_points_to_user(target, -abs(amount))
    await msg.reply(f"✅ Removed {amount} points from user `{target}`.")


# ---------- Admin: ban / unban / banlist ----------
@app.on_message(filters.command("ban"))
async def ban(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    if len(msg.command) >= 2:
        try:
            target = int(msg.command[1])
        except Exception:
            return await msg.reply("Invalid user id.")
    elif msg.reply_to_message:
        target = msg.reply_to_message.from_user.id
    else:
        return await msg.reply("Usage: `/ban <user_id>` or reply to a user's message and send `/ban`")

    await set_ban(target, True)
    await msg.reply(f"🚫 User `{target}` has been banned.")


@app.on_message(filters.command("unban"))
async def unban(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    if len(msg.command) >= 2:
        try:
            target = int(msg.command[1])
        except Exception:
            return await msg.reply("Invalid user id.")
    elif msg.reply_to_message:
        target = msg.reply_to_message.from_user.id
    else:
        return await msg.reply("Usage: `/unban <user_id>` or reply to a user's message and send `/unban`")

    await set_ban(target, False)
    await msg.reply(f"✅ User `{target}` has been unbanned.")


@app.on_message(filters.command("banlist"))
async def banlist(_, msg):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply("❌ Only the owner can use this command.")

    banned = await list_banned_users(limit=500)
    if not banned:
        return await msg.reply("No banned users.")
    text = "*Banned users:*\n" + "\n".join(str(x) for x in banned)
    await msg.reply(text)


# ---------- Ask handler (fixed and DB-backed) ----------
@app.on_message(filters.command("ask"))
async def ask(app, msg):
    uid = msg.from_user.id

    # Check banned
    #user_doc = await get_user(uid)
    #if user_doc.get("banned"):
        #return await msg.reply("🚫 You are banned from using this bot.")



    # 2) Replied message
    if msg.reply_to_message and msg.reply_to_message.text:
        question = msg.reply_to_message.text

    # 2) Inline: /ask question
    elif len(msg.command) > 1:
        question = " ".join(msg.command[1:])

    # 3) Ask for user input
    else:
        asked = await app.ask(uid, text="✍️ Send your question (or /cancel to stop):")
        question = (asked.text or "").strip()
        if not question:
            return await msg.reply("❌ No question provided. Cancelled.")

    status = await msg.reply("⏳ Thinking...")

    try:
        answer = await query_gemini(question)
    except Exception as e:
        return await status.edit_text(f"⚠️ Error: {e}")

    # increment stats (DB)
    await inc_user_stats(uid, 1)

    header = f"*Question:*\n{question}\n\n*Answer:*"
    await status.edit_text(header)

    for chunk in chunk_text(answer or ""):
        await msg.reply(chunk)


# ---------- Callback query buttons ----------
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
        user_doc = await get_user(uid)
        await cq.message.reply(f"You asked {user_doc.get('stats', 0)} questions total.")
        await cq.answer()



@app.on_message(filters.command("see"))
async def see_cmd(app, message):

    # =====================================================
    # 1) CASE: USER SENDS `/see something here`
    # =====================================================
    if len(message.command) > 1:
        user_text = " ".join(message.command[1:])
        await message.reply_text(f"🟦 **You typed:**\n`{user_text}`")
        return

    # =====================================================
    # 2) CASE: USER REPLIED TO A MESSAGE
    # =====================================================
    if message.reply_to_message:
        replied = message.reply_to_message

        if replied.text:
            await message.reply_text(f"🟧 **Replied text:**\n`{replied.text}`")
            return

        elif replied.caption:
            await message.reply_text(f"🟨 **Replied caption:**\n`{replied.caption}`")
            return

        else:
            await message.reply_text("⚠️ Replied message has no text/caption.")
            return

    # =====================================================
    # 3) FALLBACK CASE: use ask() for input
    # =====================================================
    ask_msg = await app.ask(
        chat_id=message.chat.id,
        text="👀 **Send me something to display**"
    )

    await ask_msg.reply_text(
        f"🟩 **You typed:**\n`{ask_msg.text}`"
    )




@app.on_message(filters.command("ping"))
async def ping(_, msg):
    await msg.reply("pong")

# set up DB before running app
async def _startup():
    await init_db()
    print("✅ DB initialized")
    print("🚀 Gemini Ask Bot ready.")


# run the bot with startup
if __name__ == "__main__":
    app.run()
