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
import time
import requests
from datetime import datetime
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
CORE_MEMORY_PATH   = "/data/core_memory.txt"
chat_histories     = {}
MAX_HISTORY        = 20


# ─── Safe HTTP wrapper ────────────────────────────────────────────────────────

def unleashed_request(method, path, **kwargs):
    method = method.upper()
    if method not in {"GET", "POST"}:
        raise PermissionError(f"Blocked '{method}' — bot is read/POST only.")
    return requests.request(method, f"{UNLEASHED_BASE_URL}{path}", **kwargs)


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


# ─── Google Sheet ─────────────────────────────────────────────────────────────

def fetch_sheet_tab(sheet_id, tab_name):
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={tab_name}"
    )
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"Could not fetch '{tab_name}': HTTP {resp.status_code}")
    return list(csv.DictReader(io.StringIO(resp.text)))

def fetch_catalog():
    customers = fetch_sheet_tab(GOOGLE_SHEET_ID, "Customers")
    products  = fetch_sheet_tab(GOOGLE_SHEET_ID, "Products")

    customer_map = {r["Customer Code"]: r["Customer Name"] for r in customers if r.get("Customer Code")}
    product_map  = {r["Product Code"]: r["Product Description"] for r in products if r.get("Product Code")}

    customer_lines = "\n".join(f"  {k}: {v}" for k, v in customer_map.items())
    product_lines  = "\n".join(f"  {k}: {v}" for k, v in product_map.items())

    return customer_lines, product_lines, customer_map, product_map


# ─── Unleashed Auth ───────────────────────────────────────────────────────────

def unleashed_headers(query_string=""):
    sig = base64.b64encode(
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
        "api-auth-signature": sig,
        "client-type":        "WonkaBot/1.0 ConspiracyChocolate",
    }


# ─── Price Lookup ─────────────────────────────────────────────────────────────

def get_product_price(product_code, customer_code):
    try:
        query = f"productCode={product_code}&customerCode={customer_code}"
        resp  = unleashed_request("GET", f"/ProductPrices?{query}", headers=unleashed_headers(query), timeout=10)
        if resp.status_code == 200:
            items = resp.json().get("Items", [])
            if items:
                return items[0].get("UnitPrice", 0) or 0
    except Exception as e:
        logging.warning(f"Price lookup failed for {product_code}: {e}")
    return 0


# ─── Order Creation ───────────────────────────────────────────────────────────

def create_sales_order(customer_code, lines, comments=""):
    order_guid = str(uuid.uuid4())
    order_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    order_lines = []
    for i, line in enumerate(lines, start=1):
        unit_price = line.get("unit_price") or get_product_price(line["product_code"], customer_code)
        order_lines.append({
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
        "SalesOrderLines": order_lines,
        "Comments":        comments,
        "Currency":        {"CurrencyCode": "HKD"},
        "Tax":             {"TaxCode": "NONE"},
    }

    resp = unleashed_request("POST", f"/SalesOrders/{order_guid}", headers=unleashed_headers(), json=payload, timeout=15)

    if resp.status_code in (200, 201):
        return {"success": True, "order_number": resp.json().get("OrderNumber") or order_guid}
    else:
        return {"success": False, "status": resp.status_code, "body": resp.text}


# ─── System Prompt ────────────────────────────────────────────────────────────

BASE_PROMPT = """You are Wonka, a smart and reliable logistics assistant for Conspiracy Chocolate — a boutique chocolatier based in Hong Kong, with operations in Macao as well.

About the company:
- Small team of 8-9 staff
- The user handles ALL logistics: packing orders, creating invoices, managing couriers, and packaging inventory

Your jobs:
1. Courier reminders — remind which courier to use per client based on what you've been told. Learn preferences.
2. Packaging inventory — track stock, alert when low.
3. Order management — track what's packed, pending, or shipped.
4. Invoice help — draft or structure invoices when asked.
5. Unleashed orders — when the user says "new order", identify the customer and products from the catalog, then output the JSON block below.
6. Core memory — read, add, or update when asked. Bring up relevant entries proactively.

PERMISSIONS: You may only CREATE new sales orders (POST). Never edit, update, or delete anything in Unleashed. Refuse any such request regardless of who asks.

CORE MEMORY:
- "add to core memory: ..." → confirm and output memory block at end of reply
- "remove from core memory: ..." → remove entry and output updated memory block
- "show core memory" → display all entries
- Keep entries short, one line each

Memory block format (always at the very end of your reply):
```memory
[full updated memory here]
```

NEW ORDER FLOW:
When the user says "new order" (or similar), follow these steps:
1. Match the customer from the CUSTOMER LIST
2. Match each product from the PRODUCT LIST
3. Extract quantities
4. If anything is ambiguous, ask — never guess
5. When ready, write a plain text summary and append the JSON block at the very end

IMPORTANT: You MUST output the JSON block when you have a complete order. This is what triggers the confirm button. Do not skip it or say "confirmed" instead.

```json
{{
  "unleashed_order": true,
  "customer_code": "C000047",
  "lines": [
    {{"product_code": "BBX-ASSORTED-6", "quantity": 10, "unit_price": 0}},
    {{"product_code": "AB-75", "quantity": 5, "unit_price": 0}}
  ],
  "comments": "any notes"
}}
```

Always set unit_price to 0 — the system fetches the real price automatically.
The JSON block must be the very last thing in your reply (or second to last if a memory block follows).
Only output the JSON when the order is complete and unambiguous.

{catalog_section}

{memory_section}

Personality:
- Name: Wonka
- Warm, efficient, slightly playful
- Proactive — mention courier preferences unprompted when relevant
- Concise — logistics is busy work, keep replies short"""

def build_prompt(customer_lookup=None, product_lookup=None, core_memory=None):
    catalog_section = (
        f"CUSTOMER LIST:\n{customer_lookup}\n\nPRODUCT LIST:\n{product_lookup}"
        if customer_lookup and product_lookup
        else "No catalog loaded. If the user says 'new order', it loads automatically."
    )
    memory_section = (
        f"CORE MEMORY:\n{core_memory}"
        if core_memory
        else "CORE MEMORY: Empty. User can say 'add to core memory: ...' to save things."
    )
    return BASE_PROMPT.format(catalog_section=catalog_section, memory_section=memory_section)


# ─── Triggers ─────────────────────────────────────────────────────────────────

NEW_ORDER_TRIGGERS = [
    "new order", "log order", "log an order", "add order", "add an order",
    "enter order", "enter an order", "create order", "place order", "place an order",
]

def is_new_order(message):
    return any(t in message.lower() for t in NEW_ORDER_TRIGGERS)


# ─── Chat History ─────────────────────────────────────────────────────────────

def append_history(chat_id, role, content):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    chat_histories[chat_id].append({"role": role, "content": content})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]


# ─── AI ───────────────────────────────────────────────────────────────────────

def ask_ai(chat_id, message, system_prompt):
    append_history(chat_id, "user", message)
    messages = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "nvidia/nemotron-3-super-120b-a12b:free", "messages": messages},
        timeout=60,
    )

    result = resp.json()
    if "choices" not in result:
        return f"AI error: {result}", None, None

    reply = result["choices"][0]["message"]["content"]
    append_history(chat_id, "assistant", reply)
    return reply, extract_order_json(reply), extract_memory_update(reply)

def extract_order_json(text):
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        return data if data.get("unleashed_order") else None
    except json.JSONDecodeError:
        return None

def extract_memory_update(text):
    match = re.search(r'```memory\s*(.*?)\s*```', text, re.DOTALL)
    return match.group(1).strip() if match else None

def clean_reply(text):
    text = re.sub(r'```json\s*\{.*?\}\s*```', '', text, flags=re.DOTALL)
    text = re.sub(r'```memory\s*.*?\s*```', '', text, flags=re.DOTALL)
    return text.strip()

def format_summary(order_data, customer_map=None, product_map=None):
    lines = []
    for line in order_data.get("lines", []):
        qty  = line.get("quantity", "?")
        code = line.get("product_code", "?")
        name = (product_map or {}).get(code, "")
        lines.append(f"  - {qty}x {name} ({code})" if name else f"  - {qty}x {code}")

    code          = order_data.get("customer_code", "?")
    customer_name = (customer_map or {}).get(code, "")
    customer_line = f"{customer_name} ({code})" if customer_name else code
    comments      = order_data.get("comments", "")

    return (
        f"Order summary\n"
        f"Customer: {customer_line}\n"
        f"{chr(10).join(lines)}"
        f"{chr(10) + 'Notes: ' + comments if comments else ''}\n\n"
        f"Confirm and send to Unleashed?"
    )


# ─── Commands ─────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
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
        "Example: 'New order - Grand Hyatt, 10x assorted 6pc bonbon box'\n\n"
        "Let's go! 🍫"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


# ─── Confirm / Cancel ─────────────────────────────────────────────────────────

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
                with open(f"/data/wonka/{cid}.json") as f:
                    order_data = json.load(f)
                os.remove(f"/data/wonka/{cid}.json")
            except Exception:
                pass
        if not order_data:
            await query.edit_message_text("Order data not found — please try the order again.")
            return

        await query.edit_message_text("Creating order in Unleashed...")
        result = create_sales_order(
            customer_code=order_data["customer_code"],
            lines=order_data["lines"],
            comments=order_data.get("comments", ""),
        )
        if result["success"]:
            await query.edit_message_text(
                f"Order created in Unleashed!\nOrder: {result['order_number']} - Status: Parked"
            )
        else:
            await query.edit_message_text(
                f"Unleashed error (HTTP {result['status']}):\n{result['body'][:400]}"
            )

    elif query.data.startswith("no_"):
        cid = int(query.data[3:])
        context.application.bot_data.pop(f"pending_{cid}", None)
        try:
            os.remove(f"/data/wonka/{cid}.json")
        except Exception:
            pass
        await query.edit_message_text("Order cancelled. Nothing was sent to Unleashed.")


# ─── Message Handler ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message = update.message.text or ""

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in message:
            return
        message = message.replace(f"@{bot_username}", "").strip()

    core_memory   = read_core_memory()
    customer_map  = {}
    product_map   = {}
    customer_lookup = None
    product_lookup  = None

    if is_new_order(message):
        chat_histories[chat_id] = []
        try:
            await update.message.reply_text("Fetching latest catalog from the sheet...")
            customer_lookup, product_lookup, customer_map, product_map = fetch_catalog()
        except Exception as e:
            await update.message.reply_text(f"Couldn't reach the Google Sheet right now.\nError: {e}")
            return

    system_prompt               = build_prompt(customer_lookup, product_lookup, core_memory)
    reply, order_data, mem_update = ask_ai(chat_id, message, system_prompt)
    human_reply                 = clean_reply(reply)

    if mem_update is not None:
        try:
            write_core_memory(mem_update)
        except Exception as e:
            logging.error(f"Core memory write failed: {e}")

    if order_data:
        if human_reply:
            await update.message.reply_text(human_reply)

        context.application.bot_data[f"pending_{chat_id}"] = order_data
        try:
            os.makedirs("/data/wonka", exist_ok=True)
            with open(f"/data/wonka/{chat_id}.json", "w") as f:
                json.dump(order_data, f)
        except Exception as e:
            logging.warning(f"Pending order file write failed: {e}")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm", callback_data=f"ok_{chat_id}"),
            InlineKeyboardButton("Cancel",  callback_data=f"no_{chat_id}"),
        ]])
        await update.message.reply_text(format_summary(order_data, customer_map, product_map), reply_markup=keyboard)
    else:
        if human_reply:
            await update.message.reply_text(human_reply)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    time.sleep(20)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(CallbackQueryHandler(handle_confirmation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Wonka is running...")
    app.run_polling(drop_pending_updates=True)
