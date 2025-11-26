# bot.py
import os
import asyncio
import textwrap
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand
)
from pyrogram.enums import ParseMode

import google as genai

# Optional async MongoDB
try:
    import motor.motor_asyncio as motor
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

load_dotenv()

# === REQUIRED ENV VARIABLES ONLY ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
start_points = int(os.getenv("start_points", "50"))
bonus_points = int(os.getenv("bonus_points", "20"))

if not all([BOT_TOKEN, API_ID, API_HASH]):
    raise RuntimeError("BOT_TOKEN, API_ID, API_HASH are required!")

app = Client("gemini_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB Setup
if MONGO_AVAILABLE and MONGO_URI:
    mongo = motor.AsyncIOMotorClient(MONGO_URI)
    db = mongo.gemini_bot
    users = db.users
    stats = db.stats
    bans = db.bans
    api_keys = db.api_keys
else:
    raise RuntimeError("MongoDB (motor) and MONGO_URI are required!")

# Global active API key (admin can override)
async def get_active_api_key():
    key_doc = await api_keys.find_one({"_id": "current"})
    return key_doc["key"] if key_doc else GEMINI_API_KEY

async def configure_gemini():
    key = await get_active_api_key()
    if key:
        genai.configure(api_key=key)

# Startup Tasks
async def startup_tasks():
    await configure_gemini()
    await app.set_bot_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("ask", "Ask Gemini AI"),
        BotCommand("balance", "Check your points"),
        BotCommand("bonus", "Get daily bonus (20 points)"),
        BotCommand("refer", "Get referral link"),
        BotCommand("stats", "Bot statistics"),
        BotCommand("admin_settings", "Admin panel (owner only)"),
        BotCommand("restart", "Restart bot (admin only)"),
    ])
    if OWNER_ID:
        try:
            await app.send_message(OWNER_ID, "Gemini AI Bot is now LIVE!")
        except:
            pass

# Helper: Admin Check
def is_owner_or_admin(user_id: int):
    return user_id == OWNER_ID

# Helper: Uptime
START_TIME = datetime.now(timezone.utc)

def format_uptime():
    delta = datetime.now(timezone.utc) - START_TIME
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}h {minutes}m {seconds}s"

# === USER MANAGEMENT ===
async def get_user(user_id: int):
    user = await users.find_one({"_id": user_id})
    if not user:
        user = {
            "_id": user_id,
            "points": start_points,
            "last_bonus": None,
            "total_questions": 0,
            "join_date": datetime.now(timezone.utc)
        }
        await users.insert_one(user)
        await stats.update_one({"_id": "global"}, {"$inc": {"total_users": 1}}, upsert=True)
    return user

async def deduct_point(user_id: int):
    await users.update_one({"_id": user_id}, {
        "$inc": {"points": -1, "total_questions": 1}
    })
    await stats.update_one({"_id": "global"}, {"$inc": {"total_questions": 1}}, upsert=True)

# === ADMIN PANEL KEYBOARD ===
def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Add Points", callback_data="admin_add_points"),
            InlineKeyboardButton("Remove Points", callback_data="admin_remove_points")
        ],
        [
            InlineKeyboardButton("Ban User", callback_data="admin_ban"),
            InlineKeyboardButton("Unban User", callback_data="admin_unban")
        ],
        [
            InlineKeyboardButton("Ban List", callback_data="admin_ban_list"),
            InlineKeyboardButton("Set API Key", callback_data="admin_api_key")
        ],
        [InlineKeyboardButton("Close", callback_data="admin_close")]
    ])

# === COMMANDS ===
@app.on_message(filters.command("start"))
async def start(client: Client, msg):
    user_id = msg.from_user.id
    await get_user(user_id)
    
    ref_id = None
    if len(msg.command) > 1 and msg.command[1].startswith("ref"):
        try:
            ref_id = int(msg.command[1][3:])
            if ref_id != user_id:
                await users.update_one({"_id": ref_id}, {"$inc": {"points": 5}})
        except:
            pass

    text = (
        "<b>Welcome to Gemini AI Bot!</b>\n\n"
        "Ask anything using:\n"
        "• <code>/ask What is Python?</code>\n"
        "• Reply to any message with /ask\n"
        "• Just type /ask and send your question\n\n"
        "<i>Each question costs 1 point</i>\n"
        "Use /bonus once daily for +20 points!"
    )
    await msg.reply(text, parse_mode=ParseMode.HTML)

@app.on_message(filters.command("balance"))
async def balance(client: Client, msg):
    user = await get_user(msg.from_user.id)
    await msg.reply(f"<b>Your Points:</b> {user['points']}", parse_mode=ParseMode.HTML)

@app.on_message(filters.command("bonus"))
async def bonus(client: Client, msg):
    user = await get_user(msg.from_user.id)
    now = datetime.now(timezone.utc)
    last = user.get("last_bonus")
    
    if last:
        last_time = datetime.fromisoformat(last)
        next_available = last_time + timedelta(days=1)
        if now < next_available:
            remaining = next_available - now
            hours, remainder = divmod(remaining.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return await msg.reply(
                f"You can use /bonus again in <b>{hours}h {minutes}m</b>",
                parse_mode=ParseMode.HTML
            )
    
    await users.update_one(
        {"_id": msg.from_user.id},
        {"$inc": {"points": bonus_points}, "$set": {"last_bonus": now.isoformat()}}
    )
    await msg.reply(f"<b>Bonus claimed!</b> +{bonus_points} points added!", parse_mode=ParseMode.HTML)

@app.on_message(filters.command("refer"))
async def refer(client: Client, msg):
    bot = await client.get_me()
    link = f"https://t.me/{bot.username}?start=ref{msg.from_user.id}"
    await msg.reply(
        f"<b>Your Referral Link:</b>\n{link}\n\n"
        f"Earn <b>5 points</b> when someone joins using your link!",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

@app.on_message(filters.command("stats"))
async def stats_cmd(client: Client, msg):
    user = await get_user(msg.from_user.id)
    global_stats = await stats.find_one({"_id": "global"}) or {"total_users": 0, "total_questions": 0}
    
    await msg.reply(
        "<b>Bot Statistics</b>\n\n"
        f"Total Users: <b>{global_stats['total_users']}</b>\n"
        f"Total Questions: <b>{global_stats['total_questions']}</b>\n"
        f"Uptime: <b>{format_uptime()}</b>\n"
        f"Your Asks: <b>{user['total_questions']}</b>",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("ask") & filters.private)
async def ask_command(client: Client, msg):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    
    if user["points"] <= 0:
        return await msg.reply("You have no points left!\nUse /bonus (once daily) or ask admin for points.")

    question = None
    if len(msg.command) > 1:
        question = " ".join(msg.command[1:])
    elif msg.reply_to_message and msg.reply_to_message.text:
        question = msg.reply_to_message.text
    else:
        await msg.reply("Please send your question now...")
        try:
            response = await client.ask(chat_id=msg.chat.id, user_id=user_id, timeout=120)
            question = response.text
        except asyncio.TimeoutError:
            return await msg.reply("Timeout. Please try /ask again.")

    if not question:
        return await msg.reply("No question provided.")

    await deduct_point(user_id)
    thinking = await msg.reply("<i>Thinking...</i>", parse_mode=ParseMode.HTML)

    try:
        await configure_gemini()
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = await asyncio.to_thread(model.generate_content, question)
        answer = response.text
    except Exception as e:
        return await thinking.edit_text(f"<b>Error:</b> {str(e)}", parse_mode=ParseMode.HTML)

    # Clean formatting
    header = f"<b>Question:</b>\n{question}\n\n<b>Answer:</b>\n"
    full_text = header + answer

    if len(full_text) > 4096:
        parts = textwrap.wrap(full_text, 4000, replace_whitespace=False)
        await thinking.delete()
        for i, part in enumerate(parts):
            await msg.reply(part if i == 0 else part, parse_mode=ParseMode.HTML)
    else:
        await thinking.edit_text(full_text, parse_mode=ParseMode.HTML)

# === ADMIN COMMANDS ===
@app.on_message(filters.command("admin_settings"))
async def admin_panel(client: Client, msg):
    if not is_owner_or_admin(msg.from_user.id):
        return await msg.reply("Unauthorized.")
    await msg.reply("Admin Settings", reply_markup=admin_panel_keyboard())

@app.on_message(filters.command("restart"))
async def restart_bot(client: Client, msg):
    if not is_owner_or_admin(msg.from_user.id):
        return
    await msg.reply("Restarting bot...")
    os.execv(__file__, ['python'] + [__file__])

# === CALLBACK QUERIES ===
@app.on_callback_query()
async def admin_callbacks(client: Client, cq):
    if not is_owner_or_admin(cq.from_user.id):
        return await cq.answer("Not authorized.", show_alert=True)

    data = cq.data

    if data == "admin_close":
        await cq.message.delete()
        return await cq.answer()

    if data == "admin_add_points":
        await cq.message.reply("Send: <user_id> <amount>")
        resp = await client.ask(cq.message.chat.id, cq.from_user.id)
        try:
            uid, amt = map(int, resp.text.split()[:2])
            await users.update_one({"_id": uid}, {"$inc": {"points": amt}})
            await cq.message.reply(f"Added {amt} points to {uid}")
        except:
            await cq.message.reply("Invalid format.")

    elif data == "admin_remove_points":
        await cq.message.reply("Send: <user_id> <amount>")
        resp = await client.ask(cq.message.chat.id, cq.from_user.id)
        try:
            uid, amt = map(int, resp.text.split()[:2])
            await users.update_one({"_id": uid}, {"$inc": {"points": -amt}})
            await cq.message.reply(f"Removed {amt} points from {uid}")
        except:
            await cq.message.reply("Invalid format.")

    elif data == "admin_ban":
        await cq.message.reply("Send: <user_id> [reason]")
        resp = await client.ask(cq.message.chat.id, cq.from_user.id)
        parts = resp.text.split(maxsplit=1)
        uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "No reason"
        await bans.update_one({"_id": uid}, {"$set": {"reason": reason, "banned_at": datetime.now(timezone.utc)}}, upsert=True)
        await cq.message.reply(f"User {uid} banned.")

    elif data == "admin_unban":
        await cq.message.reply("Send: <user_id>")
        resp = await client.ask(cq.message.chat.id, cq.from_user.id)
        uid = int(resp.text)
        await bans.delete_one({"_id": uid})
        await cq.message.reply(f"User {uid} unbanned.")

    elif data == "admin_ban_list":
        banned = [doc async for doc in bans.find({})]
        if not banned:
            return await cq.message.reply("No banned users.")
        text = "<b>Banned Users:</b>\n" + "\n".join([f"• {d['_id']} — {d.get('reason','No reason')}" for d in banned])
        await cq.message.reply(text, parse_mode=ParseMode.HTML)

    elif data == "admin_api_key":
        await cq.message.reply("Send new Gemini API key:")
        resp = await client.ask(cq.message.chat.id, cq.from_user.id)
        new_key = resp.text.strip()
        await api_keys.replace_one({"_id": "current"}, {"key": new_key}, upsert=True)
        await configure_gemini()
        await cq.message.reply("API key updated and applied!")

    await cq.answer()

# === START BOT ===
print("Gemini AI Bot Starting...")
app.run(startup_tasks())
