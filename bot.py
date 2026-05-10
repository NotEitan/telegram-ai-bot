import os
import logging
import requests
import base64
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

chat_histories = {}

SYSTEM_PROMPT = """You are a personal life assistant named Jt. You help with every aspect of the user's personal life.

About the user:
- Based in Hong Kong
- Daily calorie goal: 2000 calories (recently lowered from 2200, trying to lose body fat)
- Has an Apple Watch, uses Bevel and MyFitnessPal for health tracking
- Runs a business involving orders and shipments
- Wants help with health, fitness, planning, organisation, emails, decisions, journaling, and life in general

When the user sends a photo of food:
- Estimate calories and macros (protein, carbs, fat)
- Give a range e.g. "approximately 450-550 calories"
- Track running daily total when asked

For everything else:
- Draft emails, messages, or responses when asked
- Help plan trips, events, schedules and decisions
- Give advice on fitness, nutrition, sleep and recovery
- Help organise thoughts, make to-do lists, brainstorm
- Read and respond to journal entries thoughtfully
- Be proactive with suggestions when relevant
- Remember context from the conversation and refer back to it
- Be like a brilliant, reliable, discreet personal assistant who knows the user well

Keep responses concise, warm and practical. Never be preachy."""

def ask_ai(chat_id, message, image_base64=None):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    
    if image_base64:
        content = [
            {"type": "text", "text": message or "What food is this? Please estimate the calories and macros."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]
    else:
        content = message
    
    chat_histories[chat_id].append({"role": "user", "content": content})
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_histories[chat_id]
    
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "nvidia/nemotron-3-super-120b-a12b:free",
            "messages": messages
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
    message = update.message.text if update.message.text else ""

    if update.effective_chat.type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in message:
            return
        message = message.replace(f"@{bot_username}", "").strip()

    if message.startswith("Here is my Apple Health data"):
        message = "Please analyze this health data and give me a concise summary with insights and recommendations:\n\n" + message

    reply = ask_ai(chat_id, message)
    await update.message.reply_text(reply)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    file_bytes = bytes(await file.download_as_bytearray())
    image_base64 = base64.b64encode(file_bytes).decode('utf-8')
    
    caption = update.message.caption or ""
    reply = ask_ai(chat_id, caption, image_base64=image_base64)
    await update.message.reply_text(reply)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot is running...")
    app.run_polling()
