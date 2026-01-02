import os
import re
import sys
import requests

# =========================================================
# CONFIG
# =========================================================

BASE_FOLDER = "/Dropbox/1 daniyal/Auto/send to customer"  # Dropbox-mounted path OR synced path
LIFAFAY_ENDPOINT = "https://lifafay.herokuapp.com/api/design/action"

# =========================================================
# LOGGING
# =========================================================

def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()

# =========================================================
# HELPERS
# =========================================================

def normalize_phone(p: str) -> str:
    """
    Removes +, -, spaces, (), etc
    Keeps digits only
    """
    return re.sub(r"\D", "", p or "")

# =========================================================
# STEP 1 — INTENT (PHASE 1 ONLY)
# =========================================================

def detect_alignment_intent(text: str):
    dlog(f"detect_alignment_intent called with text='{text}'")

    t = text.lower()

    if any(k in t for k in ["center", "centre", "beech", "center kar", "center karo"]):
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

# =========================================================
# STEP 2 — FIND ORDER FOLDER (PHONE ONLY)
# =========================================================

def find_order_folder_by_phone(phone: str):
    dlog("find_order_folder_by_phone called")
    dlog(f"phone={phone}")

    if not os.path.exists(BASE_FOLDER):
        dlog(f"❌ BASE_FOLDER does not exist: {BASE_FOLDER}")
        return None

    target_phone = normalize_phone(phone)
    matched = []

    for folder in os.listdir(BASE_FOLDER):
        folder_norm = normalize_phone(folder)

        if target_phone and target_phone in folder_norm:
            matched.append(os.path.join(BASE_FOLDER, folder))

    dlog(f"Matched folders count = {len(matched)}")
    dlog(f"Matched folders = {matched}")

    if not matched:
        dlog("❌ No folder found for this phone")
        return None

    # If multiple, take the latest (Dropbox usually sorts by name; OK for phase 1)
    return matched[0]

# =========================================================
# MAIN ENTRY — PHASE 1
# =========================================================

def handle_design_reply(
    phone: str,
    customer_text: str,
    reply_caption: str,
    reply_whatsapp_id: str
):
    dlog("==============================================")
    dlog("handle_design_reply STARTED")
    dlog(f"phone={phone}")
    dlog(f"customer_text='{customer_text}'")
    dlog(f"reply_caption='{reply_caption}'")
    dlog(f"reply_whatsapp_id={reply_whatsapp_id}")

    # 1️⃣ Intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ EXIT: No alignment intent")
        return False

    dlog(f"Alignment intent → {alignment}")

    # 2️⃣ Folder (PHONE ONLY)
    folder_path = find_order_folder_by_phone(phone)
    if not folder_path:
        dlog("❌ EXIT: Order folder not found")
        return False

    dlog(f"✅ Folder matched → {folder_path}")

    # 3️⃣ Payload to Lifafay
    payload = {
        "source": "whatsapp",
        "phone": phone,
        "folder_path": folder_path,
        "action": "move_text_alignment",
        "alignment": alignment,
        "reply_whatsapp_id": reply_whatsapp_id,
        "caption": reply_caption
    }

    dlog("📡 Sending payload to Lifafay")
    dlog(payload)

    try:
        resp = requests.post(
            LIFAFAY_ENDPOINT,
            json=payload,
            timeout=10
        )

        dlog(f"📡 Lifafay response status={resp.status_code}")
        dlog(f"📡 Lifafay response body={resp.text}")

        if resp.status_code != 200:
            dlog("❌ Lifafay returned non-200")
            return False

    except Exception as e:
        dlog(f"❌ Lifafay request failed: {e}")
        return False

    dlog("✅ Design reply handed off to Lifafay successfully")
    return True
