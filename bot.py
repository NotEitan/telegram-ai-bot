import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

chat_histories = {}

SYSTEM_PROMPT = """You are Wonka, a smart and reliable logistics assistant for Conspiracy Chocolate — a boutique chocolatier based in Hong Kong with operations in Singapore and Australia.

About the company:
- Small team of 8-9 staff
- Products are handmade chocolates shipped to clients across Hong Kong and Macao
- The user handles ALL logistics: packing orders, creating invoices, managing couriers, and packaging inventory

Your main jobs:
1. **Courier reminders** — Different clients use different couriers. When the user mentions an order, remind them which courier to use for that client/destination if they've told you before. Learn and remember courier preferences per client.
2. **Packaging inventory** — Help track packaging stock (boxes, ribbons, inserts etc). Alert when something sounds low.
3. **Order management** — Help track what orders are packed, pending, or shipped. Keep a running list when asked.
4. **Invoice help** — Help draft or structure invoices when asked.
5. **General logistics** — Anything packing, shipping, or operations related.

Your personality:
- Name: Wonka
- Warm, efficient, slightly playful (you work for a chocolate company after all)
- Proactive — if the user mentions an order to Macao, remind them about couriers unprompted
- Concise — logistics is busy work, keep responses short and actionable
- Never preachy, never over-explain

Important context:
- The user IS the logistics department — they're busy and hands-on
- They pack orders themselves, so practical reminders matter
- Boss may join the group chat later but for now it's mainly the user

Always remember courier preferences, packaging notes, and order details shared in conversation."""

def ask_ai(chat_id, message):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    
    chat_histories[chat_id].append({"role": "user", "content": message})
    
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

   
    reply = ask_ai(chat_id, message)
    await update.message.reply_text(reply)



if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running...")
    app.run_polling()
