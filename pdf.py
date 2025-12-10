import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")                # Telegram Bot Token
url = os.getenv("url")  # Render endpoint: https://yourapp.onrender.com/upload

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF and I’ll give you an online viewer link.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document.mime_type.endswith("pdf"):
        await update.message.reply_text("Please send a PDF file.")
        return

    file_id = document.file_id
    file = await context.bot.get_file(file_id)

    # Download PDF temporarily
    pdf_bytes = requests.get(file.file_path).content

    # Upload PDF to your Render server
    response = requests.post(
        url,
        files={"file": ("document.pdf", pdf_bytes, "application/pdf")}
    )

    if response.status_code != 200:
        await update.message.reply_text("Upload failed. Try again later.")
        return

    data = response.json()
    viewer_url = data.get("viewer_url")

    await update.message.reply_text(
        f"Here is your online PDF viewer link:\n\n{viewer_url}"
    )


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(MessageHandler(filters.CommandStart(), start))

    application.run_polling()


if __name__ == "__main__":
    main()
