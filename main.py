"""
Zeapl.ai WhatsApp OCR Ordering Bot — FastAPI Backend
=====================================================
Handles WhatsApp Cloud API webhooks, Google Vision OCR,
LLM-based text structuring, fuzzy catalog matching, and session state.

Tech: FastAPI + Google Vision API + Anthropic Claude LLM
"""

import os, re, json, httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from typing import Optional
import difflib

app = FastAPI(title="Zeapl.ai WhatsApp Bot", version="1.0.0")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN   = os.getenv("WHATSAPP_TOKEN", "YOUR_WHATSAPP_TOKEN")
VERIFY_TOKEN     = os.getenv("VERIFY_TOKEN",   "zeapl_verify_secret")
PHONE_NUMBER_ID  = os.getenv("PHONE_NUMBER_ID","YOUR_PHONE_NUMBER_ID")
GOOGLE_API_KEY   = os.getenv("GOOGLE_API_KEY", "YOUR_GOOGLE_VISION_KEY")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_KEY",  "YOUR_ANTHROPIC_KEY")

WA_API_BASE = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
VISION_URL  = "https://vision.googleapis.com/v1/images:annotate"
CLAUDE_URL  = "https://api.anthropic.com/v1/messages"

# ─── CATALOG ─────────────────────────────────────────────────────────────────
CATALOG = [
    {"id": "milk_1L",     "name": "Milk 1L",        "keywords": ["milk","1l","1 litre"], "price": 52,  "icon": "🥛"},
    {"id": "milk_500ml",  "name": "Milk 500ml",      "keywords": ["milk","500ml","half litre"], "price": 28,  "icon": "🥛"},
    {"id": "bread_white", "name": "Bread White",     "keywords": ["bread","white bread"],          "price": 35,  "icon": "🍞"},
    {"id": "bread_brown", "name": "Bread Brown",     "keywords": ["bread","brown bread"],          "price": 40,  "icon": "🍞"},
    {"id": "eggs_6",      "name": "Eggs ×6",         "keywords": ["egg","eggs","6 eggs"],          "price": 42,  "icon": "🥚"},
    {"id": "eggs_12",     "name": "Eggs ×12",        "keywords": ["egg","eggs","12 eggs","dozen"], "price": 80,  "icon": "🥚"},
    {"id": "surf_1kg",    "name": "Surf Excel 1kg",  "keywords": ["surf","surfexcel","1kg","detergent"], "price": 195, "icon": "🧺"},
    {"id": "surf_500g",   "name": "Surf Excel 500g", "keywords": ["surf","surfexcel","500g"],       "price": 110, "icon": "🧺"},
    {"id": "rice_1kg",    "name": "Rice 1kg",        "keywords": ["rice","1kg"],                   "price": 65,  "icon": "🌾"},
    {"id": "rice_5kg",    "name": "Rice 5kg",        "keywords": ["rice","5kg"],                   "price": 295, "icon": "🌾"},
]

# ─── SESSION STATE ────────────────────────────────────────────────────────────
# In production: use Redis. For demo: in-memory dict.
sessions: dict[str, dict] = {}

def get_session(phone: str) -> dict:
    if phone not in sessions:
        sessions[phone] = {
            "state": "idle",
            "cart": [],
            "pending_conflicts": [],
            "conflict_index": 0,
            "raw_ocr": "",
            "structured_items": [],
        }
    return sessions[phone]

# ─── WEBHOOK VERIFICATION ─────────────────────────────────────────────────────
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")

# ─── WEBHOOK RECEIVER ─────────────────────────────────────────────────────────
@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    body = await request.json()
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]["value"]
        message = changes["messages"][0]
        phone   = message["from"]
        msg_id  = message["id"]
        bg.add_task(handle_message, phone, message)
    except (KeyError, IndexError):
        pass  # ignore status updates / delivery receipts
    return {"status": "ok"}

# ─── MESSAGE HANDLER ──────────────────────────────────────────────────────────
async def handle_message(phone: str, message: dict):
    session = get_session(phone)
    msg_type = message.get("type")

    if msg_type == "image":
        await handle_image(phone, session, message)

    elif msg_type == "interactive":
        await handle_interactive(phone, session, message)

    elif msg_type == "text":
        text = message["text"]["body"].strip().lower()
        if text in ("hi", "hello", "hey", "start"):
            await send_text(phone, (
                "👋 Welcome to *Zeapl.ai Smart Store!*\n\n"
                "Just send me a photo of your handwritten shopping list "
                "and I'll build your cart in seconds! 🛒"
            ))
            session["state"] = "waiting_image"
        elif text == "reset":
            sessions.pop(phone, None)
            await send_text(phone, "🔄 Session reset. Send 'Hi' to start again!")
        else:
            await send_text(phone, "📷 Please send an *image* of your shopping list to get started!")

# ─── IMAGE HANDLER ────────────────────────────────────────────────────────────
async def handle_image(phone: str, session: dict, message: dict):
    await send_text(phone, "📸 Got your image! Running OCR... just a moment ⏳")

    # 1. Download image from WhatsApp
    media_id  = message["image"]["id"]
    image_b64 = await download_wa_image(media_id)

    # 2. Google Vision OCR
    raw_text = await run_vision_ocr(image_b64)
    session["raw_ocr"] = raw_text
    await send_text(phone, f"🔍 *OCR complete!*\n\nExtracted text:\n```{raw_text}```")

    # 3. LLM structuring
    structured = await structure_with_llm(raw_text)
    session["structured_items"] = structured

    items_preview = "\n".join(
        [f"  • {i['product']} × {i['quantity']}" for i in structured]
    )
    await send_text(phone, f"🤖 *AI structured {len(structured)} items:*\n\n{items_preview}")

    # 4. Catalog matching
    matched, conflicts = match_catalog(structured)
    for item in matched:
        session["cart"].append(item)

    session["pending_conflicts"] = conflicts
    session["conflict_index"]    = 0
    session["state"]             = "resolving" if conflicts else "cart_ready"

    if matched:
        matched_preview = "\n".join(
            [f"  {m['icon']} {m['name']} × {m['qty']} — ₹{m['price'] * m['qty']}" for m in matched]
        )
        await send_text(phone, f"✅ *Auto-added to cart:*\n\n{matched_preview}")

    if conflicts:
        await resolve_next_conflict(phone, session)
    else:
        await show_cart(phone, session)

# ─── CONFLICT RESOLUTION ──────────────────────────────────────────────────────
async def resolve_next_conflict(phone: str, session: dict):
    idx = session["conflict_index"]
    conflicts = session["pending_conflicts"]

    if idx >= len(conflicts):
        session["state"] = "cart_ready"
        await show_cart(phone, session)
        return

    conflict = conflicts[idx]
    original = conflict["original"]
    suggestions = conflict["suggestions"]

    rows = [
        {"id": f"pick_{i}", "title": s["name"][:24], "description": f"₹{s['price']}"}
        for i, s in enumerate(suggestions[:3])
    ]
    rows.append({"id": "skip", "title": "Skip this item", "description": "Don't add to cart"})

    await send_list_message(
        phone,
        body=f"⚠️ We couldn't match *\"{original}\"*. Did you mean:",
        button_text="Choose item",
        sections=[{"title": "Suggestions", "rows": rows}]
    )

async def handle_interactive(phone: str, session: dict, message: dict):
    itype = message["interactive"]["type"]

    if itype == "list_reply":
        reply_id    = message["interactive"]["list_reply"]["id"]
        session_idx = session["conflict_index"]
        conflicts   = session["pending_conflicts"]

        if reply_id == "skip":
            session["conflict_index"] += 1
        elif reply_id.startswith("pick_"):
            pick_i   = int(reply_id.split("_")[1])
            conflict = conflicts[session_idx]
            chosen   = conflict["suggestions"][pick_i]
            session["cart"].append({**chosen, "qty": conflict["qty"]})
            session["conflict_index"] += 1

        await resolve_next_conflict(phone, session)

    elif itype == "button_reply":
        reply_id = message["interactive"]["button_reply"]["id"]

        if reply_id == "confirm_order":
            await place_order(phone, session)
        elif reply_id == "edit_cart":
            await send_text(phone, "✏️ Edit functionality available in full version. Tap *Confirm Order* to proceed.")
            await show_cart(phone, session)
        elif reply_id == "pay_upi":
            await finish_order(phone, session, "UPI")
        elif reply_id == "pay_cod":
            await finish_order(phone, session, "Cash on Delivery")

# ─── CART & CHECKOUT ─────────────────────────────────────────────────────────
async def show_cart(phone: str, session: dict):
    cart  = session["cart"]
    total = sum(c["price"] * c["qty"] for c in cart)
    rows  = "\n".join([f"  {c['icon']} {c['name']} ×{c['qty']} — ₹{c['price'] * c['qty']}" for c in cart])
    msg   = f"🛒 *Your Cart*\n\n{rows}\n\n*Total: ₹{total}*"
    await send_text(phone, msg)
    await send_buttons(phone, "What would you like to do?", [
        {"type": "reply", "reply": {"id": "confirm_order", "title": "✅ Confirm Order"}},
        {"type": "reply", "reply": {"id": "edit_cart",    "title": "✏️ Edit Cart"}},
    ])

async def place_order(phone: str, session: dict):
    total = sum(c["price"] * c["qty"] for c in session["cart"])
    await send_text(phone, f"💳 *Total: ₹{total}*\n\nChoose payment method:")
    await send_buttons(phone, "Select payment:", [
        {"type": "reply", "reply": {"id": "pay_upi", "title": "💳 Pay Now (UPI)"}},
        {"type": "reply", "reply": {"id": "pay_cod", "title": "💵 Cash on Delivery"}},
    ])

async def finish_order(phone: str, session: dict, method: str):
    import random
    order_id = f"ZPL{random.randint(10000,99999)}"
    cart  = session["cart"]
    total = sum(c["price"] * c["qty"] for c in cart)
    receipt = "\n".join([f"  {c['icon']} {c['name']} ×{c['qty']} — ₹{c['price']*c['qty']}" for c in cart])
    await send_text(phone, (
        f"🎊 *Order Confirmed!*\n\n"
        f"*Order ID:* #{order_id}\n"
        f"*Payment:* {method}\n\n"
        f"{receipt}\n\n"
        f"*Total: ₹{total}*\n\n"
        f"⏱ Estimated delivery: *45–60 minutes*\n\n"
        f"Thank you for shopping with *Zeapl.ai!* 🙏"
    ))
    sessions.pop(phone, None)

# ─── OCR — GOOGLE VISION ─────────────────────────────────────────────────────
async def download_wa_image(media_id: str) -> str:
    """Download image from WhatsApp and return base64."""
    import base64
    async with httpx.AsyncClient() as client:
        meta = await client.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        )
        url = meta.json()["url"]
        img = await client.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
        return base64.b64encode(img.content).decode()

async def run_vision_ocr(image_b64: str) -> str:
    """Call Google Vision API for handwriting detection."""
    payload = {
        "requests": [{
            "image": {"content": image_b64},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {"languageHints": ["en"]}
        }]
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{VISION_URL}?key={GOOGLE_API_KEY}",
            json=payload,
            timeout=15
        )
        data = resp.json()
        try:
            return data["responses"][0]["fullTextAnnotation"]["text"].strip()
        except (KeyError, IndexError):
            return ""

# ─── LLM STRUCTURING — CLAUDE ────────────────────────────────────────────────
async def structure_with_llm(raw_text: str) -> list[dict]:
    """Use Claude to parse noisy OCR into structured items."""
    prompt = f"""
You are parsing a handwritten shopping list extracted via OCR. The text may contain errors.
Extract all items and quantities as a JSON array. Format:
[{{"product": "item name", "quantity": number_or_string}}]

Rules:
- Normalize spelling (e.g. "Sarf Exel" → keep as-is so fuzzy matching can handle it)
- If quantity missing, assume 1
- Return ONLY valid JSON array, nothing else

OCR text:
{raw_text}
"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            CLAUDE_URL,
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        text = resp.json()["content"][0]["text"].strip()
        try:
            text = re.sub(r"```json|```", "", text).strip()
            return json.loads(text)
        except Exception:
            # Fallback: simple line-based parse
            return simple_parse(raw_text)

def simple_parse(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z\s]+?)[\s\-–:]+(\d+(?:kg|g|L|ml)?)\s*$", line, re.I)
        if m:
            items.append({"product": m.group(1).strip(), "quantity": m.group(2).strip()})
        elif line:
            items.append({"product": line, "quantity": 1})
    return items

# ─── CATALOG MATCHING ─────────────────────────────────────────────────────────
def fuzzy_score(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

def match_catalog(structured: list[dict]) -> tuple[list, list]:
    matched   = []
    conflicts = []

    for item in structured:
        product = item["product"].lower()
        qty_raw = item.get("quantity", 1)
        try:
            qty = int(re.search(r"\d+", str(qty_raw)).group())
        except Exception:
            qty = 1

        best_score  = 0.0
        best_item   = None
        suggestions = []

        for cat in CATALOG:
            score = max(
                fuzzy_score(product, cat["name"]),
                max((fuzzy_score(product, kw) for kw in cat["keywords"]), default=0)
            )
            if score > best_score:
                best_score = score
                best_item  = cat
            if score > 0.4:
                suggestions.append((score, cat))

        suggestions.sort(key=lambda x: -x[0])
        suggestions = [s[1] for s in suggestions[:3]]

        if best_score >= 0.75 and best_item:
            matched.append({**best_item, "qty": qty})
        elif suggestions:
            conflicts.append({
                "original":    item["product"],
                "qty":         qty,
                "suggestions": suggestions
            })
        else:
            conflicts.append({
                "original":    item["product"],
                "qty":         qty,
                "suggestions": CATALOG[:3]  # fallback: show first 3
            })

    return matched, conflicts

# ─── WHATSAPP SEND HELPERS ───────────────────────────────────────────────────
async def _wa_post(payload: dict):
    async with httpx.AsyncClient() as client:
        await client.post(
            WA_API_BASE,
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type":  "application/json"
            },
            json=payload,
            timeout=10
        )

async def send_text(phone: str, text: str):
    await _wa_post({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text, "preview_url": False}
    })

async def send_buttons(phone: str, body: str, buttons: list):
    await _wa_post({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": buttons}
        }
    })

async def send_list_message(phone: str, body: str, button_text: str, sections: list):
    await _wa_post({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_text, "sections": sections}

from fastapi.responses import HTMLResponse
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <head><title>Zeapl.ai Demo</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <h1>🚀 Zeapl.ai WhatsApp OCR Bot is LIVE</h1>
            <p>The backend is running and listening for WhatsApp messages.</p>
            <p><b>Webhook URL:</b> /webhook</p>
        </body>
    </html>
    """
        }
    })

# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "Zeapl.ai WhatsApp Bot v1.0"}
