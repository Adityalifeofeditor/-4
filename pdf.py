import os
import logging
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------------
# Environment variables
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
UPLOAD_URL = os.getenv("url")  # Your backend endpoint: https://yourapp.onrender.com/upload

# -----------------------------
# Notify Owner
# -----------------------------
def notify_owner():
    if not OWNER_ID:
        logger.warning("OWNER_ID not set")
        return
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
    logger.info("Owner notified")


# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF and I’ll give you an online viewer link.")
    logger.info("User ran /start")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    file_size = document.file_size

    MAX_TELEGRAM_SIZE = 20 * 1024 * 1024
    MAX_BACKEND_SIZE = 25 * 1024 * 1024

    logger.info(f"Received PDF: {file_size} bytes")

    if file_size > MAX_TELEGRAM_SIZE:
        await update.message.reply_text(
            "⚠️ This PDF is too large. Telegram only allows <20MB."
        )
        logger.warning("PDF too large for Telegram")
        return

    # Download file
    try:
        file = await context.bot.get_file(document.file_id)
        pdf_bytes = requests.get(file.file_path).content
        logger.info("Downloaded PDF from Telegram")
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update.message.reply_text("❌ Failed to download the PDF from Telegram.")
        return

    if len(pdf_bytes) > MAX_BACKEND_SIZE:
        await update.message.reply_text(
            "⚠️ Your PDF is too large for the server. Max 25MB."
        )
        logger.warning("PDF too large for backend")
        return

    # Upload to backend
    try:
        response = requests.post(
            UPLOAD_URL,
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
            timeout=40
        )
    except Exception as e:
        logger.error(f"Backend upload error: {e}")
        await update.message.reply_text("❌ Server unreachable. Try again later.")
        return

    # Handle backend errors
    content_type = response.headers.get("Content-Type", "")
    if "text/html" in content_type:
        logger.error("Backend returned HTML instead of JSON")
        await update.message.reply_text(
            "⚠️ Server error: your file may be too big or the server crashed."
        )
        return

    if response.status_code != 200:
        logger.error(f"Backend error {response.status_code}: {response.text}")
        await update.message.reply_text("❌ Upload failed. Try again later.")
        return

    # Success
    viewer_url = response.json().get("viewer_url")
    logger.info(f"Viewer URL: {viewer_url}")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Open PDF Viewer", url=viewer_url)]
    ])

    await update.message.reply_text(
        "Your PDF is ready! Click below:",
        reply_markup=keyboard
    )

# -----------------------------
# Main
# -----------------------------
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CommandHandler("start", start))

    try:
        notify_owner()
    except Exception as e:
        logger.error(f"Notify owner failed: {e}")

    application.run_polling()


if __name__ == "__main__":
    main()
    
