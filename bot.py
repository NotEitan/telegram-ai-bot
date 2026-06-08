import os
import logging
import hashlib
import hmac
import base64
import uuid
import json
import re
import csv
import io
import requests
from datetime import datetime
import zlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
UNLEASHED_API_ID   = os.environ.get("UNLEASHED_API_ID")
UNLEASHED_API_KEY  = os.environ.get("UNLEASHED_API_KEY")
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID")

_raw_ids         = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(int(i.strip()) for i in _raw_ids.split(",") if i.strip().isdigit())

UNLEASHED_BASE_URL = "https://api.unleashedsoftware.com"

chat_histories = {}
MAX_HISTORY    = 20

CORE_MEMORY_PATH = "/tmp/core_memory.txt"


# ─── Safe HTTP wrapper ────────────────────────────────────────────────────────

ALLOWED_UNLEASHED_METHODS = {"GET", "POST"}

def unleashed_request(method, path, **kwargs):
    method = method.upper()
    if method not in ALLOWED_UNLEASHED_METHODS:
        raise PermissionError(
            f"Blocked '{method}' request to Unleashed. POST/GET only."
        )
    url = f"{UNLEASHED_BASE_URL}{path}"
    return requests.request(method, url, **kwargs)


# ─── Core Memory ─────────────────────────────────────────────────────────────

def read_core_memory():
    try:
        with open(CORE_MEMORY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def write_core_memory(content):
    os.makedirs(os.path.dirname(CORE_MEMORY_PATH), exist_ok=True)
    with open(CORE_MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write(content.strip())


# ─── Google Sheet Fetcher ─────────────────────────────────────────────────────

def fetch_sheet_tab(sheet_id, tab_name):
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={tab_name}"
    )
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch sheet tab '{tab_name}': HTTP {resp.status_code}")
    reader = csv.DictReader(io.StringIO(resp.text))
    return [row for row in reader]

def fetch_catalog():
    customers = fetch_sheet_tab(GOOGLE_SHEET_ID, "Customers")
    products  = fetch_sheet_tab(GOOGLE_SHEET_ID, "Products")

    customer_name_map = {
        r["Customer Code"]: r["Customer Name"]
        for r in customers if r.get("Customer Code")
    }
    customer_lines = "\n".join(
        f"  {code}: {name}" for code, name in customer_name_map.items()
    )
    product_name_map = {
        r["Product Code"]: r["Product Description"]
        for r in products if r.get("Product Code")
    }
    product_lines = "\n".join(
        f"  {code}: {desc}" for code, desc in product_name_map.items()
    )
    return customer_lines, product_lines, customer_name_map, product_name_map


# ─── Unleashed API Auth ───────────────────────────────────────────────────────

def unleashed_headers(query_string=""):
    signature = base64.b64encode(
        hmac.new(
            UNLEASHED_API_KEY.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return {
        "Content-Type":       "application/json",
        "Accept":             "application/json",
        "api-auth-id":        UNLEASHED_API_ID,
        "api-auth-signature": signature,
        "client-type":        "WonkaBot/1.0 ConspiracyChocolate",
    }


# ─── Price Lookup ─────────────────────────────────────────────────────────────

def get_product_price(product_code, customer_code):
    """Fetch the correct sell price for a product/customer from Unleashed."""
    try:
        query = f"productCode={product_code}&customerCode={customer_code}"
        resp = unleashed_request(
            "GET",
            f"/ProductPrices?{query}",
            headers=unleashed_headers(query),
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json().get("Items", [])
            if items:
                return items[0].get("UnitPrice", 0) or 0
    except Exception as e:
        logging.warning(f"Could not fetch price for {product_code}: {e}")
    return 0


# ─── Unleashed Order Creation ─────────────────────────────────────────────────

def create_sales_order(customer_code, lines, comments=""):
    order_guid = str(uuid.uuid4())
    order_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    sales_order_lines = []
    for i, line in enumerate(lines, start=1):
        unit_price = line.get("unit_price") or get_product_price(
            line["product_code"], customer_code
        )
        sales_order_lines.append({
            "LineNumber":    i,
            "Product":       {"ProductCode": line["product_code"]},
            "OrderQuantity": line["quantity"],
            "UnitPrice":     unit_price,
        })

    payload = {
        "Guid":            order_guid,
        "OrderDate":       order_date,
        "OrderStatus":     "Parked",
        "Customer":        {"CustomerCode": customer_code},
        "SalesOrderLines": sales_order_lines,
        "Comments":        comments,
        "Currency":        {"CurrencyCode": "HKD"},
        "Tax":             {"TaxCode": "NONE"},
    }

    resp = unleashed_request(
        "POST",
        f"/SalesOrders/{order_guid}",
        headers=unleashed_headers(),
        json=payload,
        timeout=15,
    )

    if resp.status_code in (200, 201):
        data         = resp.json()
        order_number = data.get("OrderNumber") or order_guid
        return {"success": True, "order_number": order_number, "guid": order_guid}
    else:
        return {"success": False, "status": resp.status_code, "body": resp.text}


# ─── System Prompt ────────────────────────────────────────────────────────────

BASE_PROMPT = """You are Wonka, a smart and reliable logistics assistant for Conspiracy Chocolate — a boutique chocolatier based in Hong Kong with operations in Singapore and Australia.

About the company:
- Small team of 8-9 staff
- Products are handmade chocolates shipped to clients across Hong Kong, Macao, Singapore, and Australia
- The user handles ALL logistics: packing orders, creating invoices, managing couriers, and packaging inventory

Your main jobs:
1. Courier reminders — remind which courier to use per client/destination based on what you've been told.
2. Packaging inventory — help track packaging stock. Alert when something sounds low.
3. Order management — help track what orders are packed, pending, or shipped.
4. Invoice help — help draft or structure invoices when asked.
5. Unleashed order creation — when the user says "new order", use the live catalog to identify customer and products, then output the JSON block below.
6. Core memory — read it, add to it, or update it when the user asks.

CRITICAL PERMISSIONS:
You are authorised to CREATE new sales orders only (POST). No editing, updating, or deleting — ever.

CORE MEMORY RULES:
- "add to core memory: ..." → save it, output memory block at end
- "remove from core memory: ..." → remove it, output updated memory block
- "show core memory" → display all entries
- Bring up relevant memory proactively
- Keep entries short, one line per item

Memory update format (at very end of reply):
```memory
[full updated memory content]
```

UNLEASHED ORDER CREATION:
When the user says "new order" (or similar), follow these steps:
1. Identify customer from CUSTOMER LIST below
2. Identify each product from PRODUCT LIST below
3. Extract quantities
4. If ambiguous, ask to clarify — never guess
5. When ready, output a plain text summary AND the JSON block at the very end

IMPORTANT: You MUST output the JSON block exactly as shown below. Do not skip it. Do not say "confirmed" instead. The JSON block is what triggers the confirm button.

```json
{
  "unleashed_order": true,
  "customer_code": "C000047",
  "lines": [
    {"product_code": "BBX-ASSORTED-6", "quantity": 10, "unit_price": 0},
    {"product_code": "AB-75", "quantity": 5, "unit_price": 0}
  ],
  "comments": "any notes"
}
```

Rules:
- Always set unit_price to 0 (the system fetches the real price automatically)
- JSON block must be the VERY LAST thing in your reply
- Only output JSON when you have a complete unambiguous order

{catalog_section}

{memory_section}

Your personality:
- Name: Wonka
- Warm, efficient, slightly playful
- Proactive — mention courier preferences unprompted when relevant
- Concise — logistics is busy work"""

def build_system_prompt(customer_lookup=None, product_lookup=None, core_memory=None):
    if customer_lookup and product_lookup:
        catalog_section = (
            f"CUSTOMER LIST (CustomerCode: CustomerName):\n{customer_lookup}\n\n"
            f"PRODUCT LIST (ProductCode: ProductDescription):\n{product_lookup}"
        )
    else:
        catalog_section = "NOTE: No catalog loaded. If user says 'new order', catalog loads automatically."

    if core_memory:
        memory_section = f"YOUR CORE MEMORY:\n{core_memory}"
    else:
        memory_section = "YOUR CORE MEMORY: Empty. User can say 'add to core memory: ...' to save things."

    return BASE_PROMPT.format(
        catalog_section=catalog_section,
        memory_section=memory_section,
    )


# ─── Trigger Detection ────────────────────────────────────────────────────────

NEW_ORDER_TRIGGERS = [
    "new order", "log order", "log an order", "add order",
    "add an order", "enter order", "enter an order", "create order",
    "place order", "place an order",
]

def is_new_order(message):
    return any(trigger in message.lower() for trigger in NEW_ORDER_TRIGGERS)


# ─── Chat History ─────────────────────────────────────────────────────────────

def append_history(chat_id, role, content):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    chat_histories[chat_id].append({"role": role, "content": content})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]


# ─── AI Layer ─────────────────────────────────────────────────────────────────

def ask_ai(chat_id, message, system_prompt):
    append_history(chat_id, "user", message)
    messages = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "model":    "nvidia/nemotron-3-super-120b-a12b:free",
            "messages": messages,
        },
        timeout=60,
    )

    result = response.json()
    if "choices" not in result:
        return f"Error from AI: {result}", None, None

    reply = result["choices"][0]["message"]["content"]
    append_history(chat_id, "assistant", reply)

    order_data    = extract_order_json(reply)
    memory_update = extract_memory_update(reply)
    return reply, order_data, memory_update


def extract_order_json(text):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        if data.get("unleashed_order"):
            return data
    except json.JSONDecodeError:
        pass
    return None


def extract_memory_update(text):
    match = re.search(r'```memory\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def clean_reply(text):
    text = re.sub(r'```json\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
    text = re.sub(r'```memory\s*.*?\s*```',   '', text, flags=re.DOTALL)
    return text.strip()


def format_order_summary(order_data, customer_name_map=None, product_name_map=None):
    lines = []
    for line in order_data.get("lines", []):
        qty  = line.get("quantity", "?")
        code = line.get("product_code", "?")
        name = (product_name_map or {}).get(code, "")
        if name:
            lines.append(f"  - {qty}x {name} ({code})")
        else:
            lines.append(f"  - {qty}x {code}")

    items         = "\n".join(lines)
    customer_code = order_data.get("customer_code", "?")
    customer_name = (customer_name_map or {}).get(customer_code, "")
    customer_line = f"{customer_name} ({customer_code})" if customer_name else customer_code
    comments      = order_data.get("comments", "")
    comment_line  = f"\nNotes: {comments}" if comments else ""

    return (
        f"Order summary\n"
        f"Customer: {customer_line}\n"
        f"{items}"
        f"{comment_line}\n\n"
        f"Confirm and send to Unleashed?"
    )


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    await update.message.reply_text(
        "Hey! I'm Wonka, the Conspiracy Chocolate logistics bot.\n\n"
        "What I can do:\n"
        "- Courier reminders per client\n"
        "- Packaging inventory tracking\n"
        "- Order management\n"
        "- Invoice help\n"
        "- Log orders to Unleashed (say 'new order')\n"
        "- Core memory (say 'add to core memory: ...')\n\n"
        "Example order: 'New order - Grand Hyatt, 10x assorted 6pc bonbon box'\n\n"
        "Let's go! 🍫"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


# ─── Confirmation Button Handler ──────────────────────────────────────────────

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await query.answer("Not authorised.")
        return

    await query.answer()

    if query.data.startswith("ok_"):
        cid        = int(query.data[3:])
        order_data = context.application.bot_data.pop(f"pending_{cid}", None)
        if not order_data:
            try:
                with open(f"/tmp/wonka/{cid}.json") as pf:
                    order_data = json.load(pf)
                os.remove(f"/tmp/wonka/{cid}.json")
            except Exception:
                pass
        if not order_data:
            await query.edit_message_text("Order data not found — please try again.")
            return

        await query.edit_message_text("Creating order in Unleashed...")
        result = create_sales_order(
            customer_code=order_data["customer_code"],
            lines=order_data["lines"],
            comments=order_data.get("comments", ""),
        )
        if result["success"]:
            await query.edit_message_text(
                f"Order created in Unleashed!\n"
                f"Order: {result['order_number']} - Status: Parked"
            )
        else:
            await query.edit_message_text(
                f"Unleashed error (HTTP {result['status']}):\n{result['body'][:400]}"
            )

    elif query.data.startswith("no_"):
        cid = int(query.data[3:])
        context.application.bot_data.pop(f"pending_{cid}", None)
        try:
            os.remove(f"/tmp/wonka/{cid}.json")
        except Exception:
            pass
        await query.edit_message_text("Order cancelled. Nothing was sent to Unleashed.")


# ─── Main Message Handler ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message = update.message.text if update.message.text else ""

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logging.warning(f"Blocked unauthorised user {user_id}")
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in message:
            return
        message = message.replace(f"@{bot_username}", "").strip()

    core_memory = read_core_memory()

    customer_lookup   = None
    product_lookup    = None
    customer_name_map = {}
    product_name_map  = {}

    if is_new_order(message):
        # Clear history on new order so AI starts fresh
        chat_histories[chat_id] = []
        try:
            await update.message.reply_text("Fetching latest catalog from the sheet...")
            customer_lookup, product_lookup, customer_name_map, product_name_map = fetch_catalog()
        except Exception as e:
            logging.error(f"Sheet fetch failed: {e}")
            await update.message.reply_text(
                f"Couldn't reach the Google Sheet right now.\nError: {e}"
            )
            return

    system_prompt                    = build_system_prompt(customer_lookup, product_lookup, core_memory)
    reply, order_data, memory_update = ask_ai(chat_id, message, system_prompt)
    human_reply                      = clean_reply(reply)

    if memory_update is not None:
        try:
            write_core_memory(memory_update)
        except Exception as e:
            logging.error(f"Failed to write core memory: {e}")

    if order_data:
        summary = format_order_summary(order_data, customer_name_map, product_name_map)

        if human_reply:
            await update.message.reply_text(human_reply)

        context.application.bot_data[f"pending_{chat_id}"] = order_data
        try:
            os.makedirs("/tmp/wonka", exist_ok=True)
            with open(f"/tmp/wonka/{chat_id}.json", "w") as pf:
                json.dump(order_data, pf)
        except Exception as e:
            logging.warning(f"Could not write pending order file: {e}")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Confirm", callback_data=f"ok_{chat_id}"),
                InlineKeyboardButton("Cancel",  callback_data=f"no_{chat_id}"),
            ]
        ])
        await update.message.reply_text(summary, reply_markup=keyboard)
    else:
        if human_reply:
            await update.message.reply_text(human_reply)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    WEBHOOK_URL = "https://telegram-ai-bot-1-ky7c.onrender.com"
    PORT        = int(os.environ.get("PORT", 10000))

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(CallbackQueryHandler(handle_confirmation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Wonka is running via webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook",
    )
