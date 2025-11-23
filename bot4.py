import os
import textwrap
import asyncio
from collections import defaultdict
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai

# Load env variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("BOT_TOKEN, API_ID, and API_HASH must be defined in .env")

# Initialize bot using your method
app = Client(
    name="ask_bot",
    bot_token=BOT_TOKEN,
    api_id=int(API_ID),
    api_hash=API_HASH,
)

# Gemini AI client
gemini = genai.Client(api_key=GEMINI_API_KEY)

# Simple state + stats
user_states = defaultdict(lambda: {"awaiting": False})
user_stats = defaultdict(int)
total_stats = 0


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


def chunk_text(text, size=3800):
    for i in range(0, len(text), size):
        yield text[i:i+size]


async def query_gemini(prompt: str):
    loop = asyncio.get_event_loop()

    def _call():
        res = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return getattr(res, "text", None) or str(res)

    return await loop.run_in_executor(None, _call)


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
        "• `/stats` — Show usage statistics\n"
    )


@app.on_message(filters.command("restart"))
async def restart(_, msg):
    user_states[msg.from_user.id]["awaiting"] = False
    await msg.reply("🔄 Session restarted. Use /ask to ask a question again.")


@app.on_message(filters.command("stats"))
async def stats(_, msg):
    uid = msg.from_user.id
    await msg.reply(
        f"*Your Stats:*\nQuestions asked: {user_stats[uid]}\n"
        f"*Total Questions:* {total_stats}"
    )


@app.on_message(filters.command("ask"))
async def ask(_, msg):
    global total_stats
    uid = msg.from_user.id

    # 1) Inline: /ask question
    if len(msg.command) > 1:
        question = " ".join(msg.command[1:])

    # 2) Replied message
    elif msg.reply_to_message and msg.reply_to_message.text:
        question = msg.reply_to_message.text

    # 3) Ask for user input
    else:
        user_states[uid]["awaiting"] = True
        return await msg.reply("✍️ Please send your question...")

    user_states[uid]["awaiting"] = False
    user_stats[uid] += 1
    total_stats += 1

    status = await msg.reply("⏳ Thinking...")

    try:
        answer = await query_gemini(question)
    except Exception as e:
        return await status.edit_text(f"⚠️ Error: {e}")

    header = f"*Question:*\n{question}\n\n*Answer:*"
    await status.edit_text(header)

    for chunk in chunk_text(answer):
        await msg.reply(chunk)


@app.on_message(filters.text)
async def catch_text(_, msg):
    uid = msg.from_user.id

    if user_states[uid]["awaiting"]:
        user_states[uid]["awaiting"] = False

        global total_stats
        user_stats[uid] += 1
        total_stats += 1

        status = await msg.reply("⏳ Thinking...")

        try:
            answer = await query_gemini(msg.text)
        except Exception as e:
            return await status.edit_text(f"⚠️ Error: {e}")

        header = f"*Question:*\n{msg.text}\n\n*Answer:*"
        await status.edit_text(header)

        for chunk in chunk_text(answer):
            await msg.reply(chunk)

    else:
        await msg.reply(
            "Use /ask to ask a question or reply to a message with /ask.",
            reply_markup=main_keyboard()
        )


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


print("🚀 Gemini Ask Bot Started")
app.run()
