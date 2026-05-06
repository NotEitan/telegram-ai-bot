import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

chat_histories = {}

def ask_ai(chat_id, message):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    
    chat_histories[chat_id].append({"role": "user", "content": message})
    
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "openrouter/auto",
            "messages": chat_histories[chat_id]
        }
    )
    
    result = response.json()
    
    if "choices" not in result:
        return f"Error: {result}"
    
    reply = result["choices"][0]["message"]["content"]
    chat_histories[chat_id].append({"role": "assistant", "content": reply})
    return reply

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message.text

    if update.effective_chat.type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in message:
            return
        message = message.replace(f"@{bot_username}", "").strip()

    reply = ask_ai(chat_id, message)
    await update.message.reply_text(reply)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()
