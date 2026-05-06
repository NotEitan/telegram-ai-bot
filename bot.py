import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

chat_histories = {}

def ask_gemini(chat_id, message):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    
    chat_histories[chat_id].append({"role": "user", "parts": [{"text": message}]})
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": chat_histories[chat_id]}
    response = requests.post(url, json=payload)
    result = response.json()
    
    if "candidates" not in result:
        return f"Error: {result}"
    
    reply = result["candidates"][0]["content"]["parts"][0]["text"]
    chat_histories[chat_id].append({"role": "model", "parts": [{"text": reply}]})
    return reply

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message.text

    if update.effective_chat.type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in message:
            return
        message = message.replace(f"@{bot_username}", "").strip()

    reply = ask_gemini(chat_id, message)
    await update.message.reply_text(reply)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()
