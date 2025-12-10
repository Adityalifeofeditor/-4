import os
import logging
import requests
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters
)

# ─────────────────────────────────────────────
# LOGGING (recommended for Render logs)
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
UPLOAD_URL = os.getenv("url")  # Render upload endpoint


# ─────────────────────────────────────────────
# OWNER NOTIFICATION
# ─────────────────────────────────────────────
def notify_owner():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": OWNER_ID,
        "text": (
            "🚀 *Bot Restarted Successfully!* ✅\n\n"
            f"📊 *Status:* Online\n"
            f"⏰ *Time:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        "parse_mode": "Markdown"
    }
    requests.post(url, data=data)
    logger.info("Owner notified.")


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF and I’ll give you an online viewer link.")
    logger.info("User ran /start")


# ─────────────────────────────────────────────
# PDF HANDLER
# ─────────────────────────────────────────────
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("PDF received from user...")
    document = update.message.document

    if not document.mime_type.endswith("pdf"):
        await update.message.reply_text("Please send a PDF file.")
        logger.warning("Non-PDF file received.")
        return

    # Download PDF from Telegram
    file = await context.bot.get_file(document.file_id)
    pdf_bytes = requests.get(file.file_path).content
    logger.info("Downloaded PDF from Telegram")

    # Upload PDF to Render backend
    try:
        response = requests.post(
            UPLOAD_URL,
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")}
        )
    except Exception as e:
        logger.error(f"Error uploading to Render: {e}")
        await update.message.reply_text("Upload failed. Server unreachable.")
        return

    if response.status_code != 200:
        logger.error(f"Backend error: {response.text}")
        await update.message.reply_text("Upload failed. Try again later.")
        return

    data = response.json()
    viewer_url = data.get("viewer_url")

    logger.info(f"Viewer link generated: {viewer_url}")

    # ─────────────────────────────────────────────
    # INLINE BUTTON WITH VIEWER LINK
    # ─────────────────────────────────────────────
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📄 Open PDF Viewer", url=viewer_url)]]
    )

    await update.message.reply_text(
        "Your PDF is ready! Click the button below:",
        reply_markup=keyboard
    )


# ─────────────────────────────────────────────
# MAIN FUNCTION
# ─────────────────────────────────────────────
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # PDF handler
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    # /start command
    application.add_handler(CommandHandler("start", start))

    # Notify owner
    try:
        notify_owner()
        print("✅ Owner notified successfully!")
    except Exception as e:
        print("⚠️ Failed notify_owner handler:", e)
        logger.error(f"Notify owner failed: {e}")

    application.run_polling()


if __name__ == "__main__":
    main()
    
