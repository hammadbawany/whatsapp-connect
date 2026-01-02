import re
import sys
import json
import requests

from app.plugins.dropbox_plugin import get_system_dropbox_client

# ===============================
# CONFIG
# ===============================

BASE_DROPBOX_FOLDER = "/1 daniyal/Auto/send to customer"
LIFAFAY_ENDPOINT = "https://lifafay.herokuapp.com/api/design/action"

# ===============================
# LOGGING
# ===============================

def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()

# ===============================
# STEP 1 — INTENT (PHASE 1 ONLY)
# ===============================

def detect_alignment_intent(text: str):
    dlog(f"detect_alignment_intent called with text='{text}'")

    t = text.lower()

    if any(k in t for k in ["center", "centre", "beech", "center karo", "center kar"]):
        dlog("✅ Alignment detected: center")
        return "center"

    if any(k in t for k in ["right", "dayen", "right side"]):
        dlog("✅ Alignment detected: right")
        return "right"

    if any(k in t for k in ["left", "baen", "left side"]):
        dlog("✅ Alignment detected: left")
        return "left"

    dlog("❌ No alignment intent detected")
    return None

# ===============================
# STEP 2 — FIND ORDER FOLDER
# ===============================

def normalize_phone(s: str):
    return re.sub(r"\D", "", s or "")

def find_order_folder(dbx, phone: str):
    dlog("find_order_folder called")
    dlog(f"phone={phone}")

    try:
        res = dbx.files_list_folder(BASE_DROPBOX_FOLDER)
    except Exception as e:
        dlog(f"❌ Dropbox list failed: {e}")
        return None

    phone_digits = normalize_phone(phone)
    matched = []

    for entry in res.entries:
        if not entry.name:
            continue

        folder_digits = normalize_phone(entry.name)

        # match both ways (handles multiple numbers, formats)
        if phone_digits in folder_digits or folder_digits in phone_digits:
            matched.append(entry.path_lower)

    dlog(f"Matched folders count = {len(matched)}")
    dlog(f"Matched folders = {matched}")

    return matched[0] if matched else None

# ===============================
# STEP 3 — HAND OFF TO LIFAFAY
# ===============================

def send_to_lifafay(payload: dict):
    dlog("📡 Sending payload to Lifafay")
    dlog(json.dumps(payload, indent=2))

    try:
        resp = requests.post(
            LIFAFAY_ENDPOINT,
            json=payload,
            timeout=15
        )

        dlog(f"📡 Lifafay response status={resp.status_code}")
        dlog(f"📡 Lifafay response body={resp.text}")

        return resp.status_code == 200

    except Exception as e:
        dlog(f"❌ Lifafay request failed: {e}")
        return False

# ===============================
# MAIN ENTRY (PHASE 1)
# ===============================

def handle_design_reply(
    phone: str,
    customer_text: str,
    reply_caption: str,
    reply_whatsapp_id: str
):
    dlog("=" * 46)
    dlog("handle_design_reply STARTED")
    dlog(f"phone={phone}")
    dlog(f"customer_text='{customer_text}'")
    dlog(f"reply_caption='{reply_caption}'")
    dlog(f"reply_whatsapp_id={reply_whatsapp_id}")

    # 1️⃣ Detect intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ EXIT: No alignment intent")
        return False

    dlog(f"Alignment intent → {alignment}")

    # 2️⃣ Dropbox client
    try:
        dbx = get_system_dropbox_client()
    except Exception as e:
        dlog(f"❌ Dropbox auth failed: {e}")
        return False

    # 3️⃣ Find folder (PHONE ONLY — AS YOU REQUIRED)
    folder_path = find_order_folder(dbx, phone)
    if not folder_path:
        dlog("❌ EXIT: Order folder not found")
        return False

    dlog(f"Matched Dropbox folder → {folder_path}")

    # 4️⃣ Hand off to Lifafay (NO EDITING HERE)
    payload = {
        "source": "whatsapp",
        "phone": phone,
        "reply_whatsapp_id": reply_whatsapp_id,
        "action": "move_text_alignment",
        "alignment": alignment,
        "dropbox_folder": folder_path,
        "caption": reply_caption
    }

    success = send_to_lifafay(payload)

    if success:
        dlog("✅ Design reply handed off to Lifafay successfully")
        return True

    dlog("❌ Lifafay handoff failed")
    return False
