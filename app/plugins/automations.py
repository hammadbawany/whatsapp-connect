# app/automations.py

import sys
import os
import json
from datetime import datetime, timedelta

# =========================
# EXISTING IMPORTS
# =========================
from app.plugins.auto_design_sender import (
    get_system_dropbox_client,
    send_file_via_meta_and_db
)

# =========================
# GPT SETUP
# =========================
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_ENABLED = bool(OPENAI_API_KEY)

client = OpenAI(api_key=OPENAI_API_KEY) if GPT_ENABLED else None

# =========================
# CONSTANTS
# =========================
FONT_DROPBOX_PATH = "/hAMMAD/LifafayAutomation/Fonts/font-options.PNG"

# =========================
# LOGGING
# =========================
def alog(msg):
    print(f"[AUTOMATION-CORE] {msg}", file=sys.stdout)
    sys.stdout.flush()

# =========================
# RULE DEFINITIONS
# =========================
AUTOMATION_RULES = {
    "font_change": {
        "keywords": [
            "font",
            "change font",
            "different font",
            "font style",
            "font option",
            "text style",
            "change text style",
            "simple font",
            "simple text"
        ],
        "cooldown_minutes": 60,
        "description": "Customer wants to change or choose a font"
    }
}

# =========================
# FAST INTENT CHECK (FREE)
# =========================
def fast_intent_detect(text: str):
    if not text:
        return None

    t = text.lower()
    for intent, cfg in AUTOMATION_RULES.items():
        for kw in cfg["keywords"]:
            if kw in t:
                return intent
    return None

# =========================
# GPT INTENT ENGINE
# =========================
def gpt_intent_detect(text: str):
    if not GPT_ENABLED:
        return None

    system_prompt = """
You are an intent classifier for customer support chats.

Rules:
- Only return valid JSON
- Only choose from the provided intents
- If unsure, return intent=null
- Be conservative
"""

    intents_list = "\n".join(
        [f"- {k}: {v['description']}" for k, v in AUTOMATION_RULES.items()]
    )

    user_prompt = f"""
Message:
"{text}"

Available intents:
{intents_list}

Return JSON:
{{ "intent": string|null, "confidence": number }}
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0,
            max_tokens=60
        )

        return json.loads(resp.choices[0].message.content)

    except Exception as e:
        alog(f"GPT ERROR: {e}")
        return None

# =========================
# SINGLE SOURCE OF TRUTH
# =========================
def detect_intent_with_confidence(text: str):
    alog("detect_intent_with_confidence called")

    # 1️⃣ FAST PATH
    intent = fast_intent_detect(text)
    if intent:
        alog(f"FAST INTENT HIT → {intent}")
        return intent, 1.0, "fast"

    # 2️⃣ GPT FALLBACK
    alog("NO FAST MATCH → CALLING GPT")

    gpt_result = gpt_intent_detect(text)
    alog(f"GPT RAW RESULT → {gpt_result}")

    if not gpt_result:
        return None, 0.0, "none"

    intent = gpt_result.get("intent")
    confidence = gpt_result.get("confidence", 0)

    alog(f"GPT intent result → {intent} (confidence={confidence})")

    return intent, confidence, "gpt"

# =========================
# COOLDOWN CHECK
# =========================
def can_trigger(cur, phone, intent, cooldown_minutes):
    cur.execute("""
        SELECT last_triggered
        FROM automation_logs
        WHERE phone = %s AND intent = %s
    """, (phone, intent))
    row = cur.fetchone()

    if not row:
        return True

    return (datetime.utcnow() - row["last_triggered"]) > timedelta(minutes=cooldown_minutes)

# =========================
# HANDLERS
# =========================
def handle_font_change(phone, send_text):
    alog("Sending font instruction text")

    send_text(
        phone,
        "Sure 😊\nPlease choose a font from the attached file and tell us the font name."
    )

    alog("Sending font file from Dropbox")

    dbx = get_system_dropbox_client()
    if not dbx:
        raise Exception("Dropbox client not available")

    _, res = dbx.files_download(FONT_DROPBOX_PATH)

    send_file_via_meta_and_db(
        phone=phone,
        file_bytes=res.content,
        filename="font-options.PNG",
        mime_type="image/png",
        caption="Font options"
    )

    alog("Font automation completed")

# =========================
# MAIN EXECUTION
# =========================
def run_automations(cur, phone, message_text, send_text, send_attachment=None):
    intent, confidence, source = detect_intent_with_confidence(message_text)

    if not intent:
        return False

    rule = AUTOMATION_RULES[intent]

    if not can_trigger(cur, phone, intent, rule["cooldown_minutes"]):
        alog("Cooldown active — skipping")
        return False

    # 🚀 AUTO SEND
    if confidence >= 0.80:
        alog(f"AUTO-SEND → {intent} ({confidence}) via {source}")

        if intent == "font_change":
            handle_font_change(phone, send_text)

    # 👀 PREVIEW ONLY
    elif confidence >= 0.50:
        alog(f"PREVIEW REQUIRED → {intent} ({confidence})")
        return False

    else:
        return False

    cur.execute("""
        INSERT INTO automation_logs (phone, intent, last_triggered)
        VALUES (%s, %s, NOW())
        ON CONFLICT (phone, intent)
        DO UPDATE SET last_triggered = NOW()
    """, (phone, intent))

    return True

# =========================
# PREVIEW (UI)
# =========================
def preview_automation(message_text):
    intent, confidence, source = detect_intent_with_confidence(message_text)

    # 🚀 AUTO-SEND → NO PREVIEW
    if confidence >= 0.80:
        return None

    # 👀 PREVIEW
    if confidence >= 0.50:
        return {
            "intent": intent,
            "confidence": confidence,
            "source": source,
            "preview_text": "Please choose a font from the attached file.",
            "preview_attachment": "font-options.PNG"
        }

    return None
