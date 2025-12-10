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

# -----------------------------------------------------
# LOGGING SETUP
# -----------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------
# ENVIRONMENT VARIABLES
# -----------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
UPLOAD_URL = os.getenv("url")  # Your Render backend endpoint


# -----------------------------------------------------
# NOTIFY OWNER ON STARTUP
# -----------------------------------------------------
def notify_owner():
    if not OWNER_ID:
        logger.warning("OWNER_ID not set. Cannot notify.")
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
    logger.info("Owner notified about restart.")


# -----------------------------------------------------
# START COMMAND
# -----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF and I’ll give you an online viewer link.")
    logger.info("User executed /start")


# -----------------------------------------------------
# PDF HANDLER — WITH ALL FIXES
# -----------------------------------------------------
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    file_size = document.file_size

    MAX_TELEGRAM_SIZE = 20 * 1024 * 1024     # 20 MB = Telegram limit
    MAX_BACKEND_SIZE  = 25 * 1024 * 1024     # Render typical limit before returning HTML error

    logger.info(f"Received PDF size: {file_size} bytes")

    # -----------------------------------------------------
    # 1️⃣ TELEGRAM FILE SIZE LIMIT (must be < 20MB)
    # -----------------------------------------------------
    if file_size > MAX_TELEGRAM_SIZE:
        await update.message.reply_text(
            "⚠️ This PDF is too large.\n"
            "Telegram only allows bots to download files *up to 20MB*.\n"
            "Please send a smaller PDF."
        )
        logger.warning("PDF rejected: too large for Telegram")
        return

    # -----------------------------------------------------
    # 2️⃣ DOWNLOAD FROM TELEGRAM
    # -----------------------------------------------------
    try:
        file = await context.bot.get_file(document.file_id)
        pdf_bytes = requests.get(file.file_path).content
        logger.info("Downloaded PDF from Telegram successfully")
    except Exception as e:
        logger.error(f"Download error: {e}")
        await update.message.reply_text("❌ Failed to download the PDF from Telegram.")
        return

    # -----------------------------------------------------
    # 3️⃣ CHECK BACKEND SIZE LIMIT BEFORE UPLOADING
    # -----------------------------------------------------
    if len(pdf_bytes) > MAX_BACKEND_SIZE:
        await update.message.reply_text(
            "⚠️ Your file is too large for the server (Render) to process.\n"
            "Please upload a PDF under 25MB."
        )
        logger.warning("PDF rejected: too large for backend")
        return

    # -----------------------------------------------------
    # 4️⃣ UPLOAD TO BACKEND
    # -----------------------------------------------------
    try:
        response = requests.post(
            UPLOAD_URL,
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
            timeout=60
        )
    except Exception as e:
        logger.error(f"Render upload error: {e}")
        await update.message.reply_text(
            "❌ Server unreachable right now. Try again later."
        )
        return

    # -----------------------------------------------------
    # 5️⃣ BACKEND RETURNED HTML (ERROR) 💥
    # -----------------------------------------------------
    content_type = response.headers.get("Content-Type", "")

    if "text/html" in content_type:
        logger.error("Backend returned HTML instead of JSON. Likely crash or file too large.")
        await update.message.reply_text(
            "⚠️ Server error: your file might be too big or the server crashed.\n"
            "Please try with a smaller PDF."
        )
        return

    # -----------------------------------------------------
    # 6️⃣ NON-200 STATUS CODE
    # -----------------------------------------------------
    if response.status_code != 200:
        logger.error(f"Backend error {response.status_code}: {response.text}")
        await update.message.reply_text(
            "❌ Upload failed. Server returned an error."
        )
        return

    # -----------------------------------------------------
    # 7️⃣ SUCCESS — GET JSON + BUTTON
    # -----------------------------------------------------
    data = response.json()
    viewer_url = data.get("viewer_url")

    logger.info(f"Viewer URL: {viewer_url}")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Open PDF Viewer", url=viewer_url)]
    ])

    await update.message.reply_text(
        "Your PDF is ready! Click below:",
        reply_markup=keyboard
    )


# -----------------------------------------------------
# MAIN APP
# -----------------------------------------------------
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CommandHandler("start", start))

    # Startup notification
    try:
        notify_owner()
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

    application.run_polling()


if __name__ == "__main__":
    main()
    
