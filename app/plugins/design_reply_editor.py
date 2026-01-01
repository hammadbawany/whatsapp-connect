import os
import re
import sys
import requests

BASE_FOLDER = r"D:\Dropbox\1 daniyal\Auto\send to customer"

# 🔗 Lifafay API (OTHER SERVER)
LIFAFAY_API_URL = "https://lifafay.yourdomain.com/api/design/edit"


def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()


# ------------------------------------------------
# STEP 1 — DETECT ALIGNMENT INTENT
# ------------------------------------------------

def detect_alignment_intent(text: str):
    dlog(f"detect_alignment_intent called with text='{text}'")

    if not text:
        dlog("❌ Empty text received")
        return None

    t = text.lower()

    if any(k in t for k in [
        "center", "centre", "middle",
        "beech", "center kar", "center karo"
    ]):
        dlog("✅ Alignment detected: center")
        return "center"

    if any(k in t for k in [
        "right", "right side", "dayen"
    ]):
        dlog("✅ Alignment detected: right")
        return "right"

    if any(k in t for k in [
        "left", "left side", "baen"
    ]):
        dlog("✅ Alignment detected: left")
        return "left"

    dlog("❌ No alignment keyword matched")
    return None


# ------------------------------------------------
# STEP 2 — FIND ORDER FOLDER
# ------------------------------------------------

def find_order_folder(phone: str, caption: str):
    dlog(f"find_order_folder called")
    dlog(f"phone={phone}")
    dlog(f"caption='{caption}'")

    order_match = re.search(r"\b\d{4,6}\b", caption or "")
    order_code = order_match.group(0) if order_match else None

    dlog(f"Extracted order_code={order_code}")

    if not os.path.exists(BASE_FOLDER):
        dlog(f"❌ BASE_FOLDER does not exist: {BASE_FOLDER}")
        return None

    for folder in os.listdir(BASE_FOLDER):
        dlog(f"Checking folder: {folder}")

        if phone not in folder:
            continue

        if order_code and order_code not in folder:
            dlog(f"Skipping folder (order mismatch): {folder}")
            continue

        matched = os.path.join(BASE_FOLDER, folder)
        dlog(f"✅ Matched order folder: {matched}")
        return matched

    dlog("❌ No matching order folder found")
    return None


# ------------------------------------------------
# STEP 3 — FIND SVG FILE
# ------------------------------------------------

def find_svg_from_folder(folder_path: str):
    dlog(f"find_svg_from_folder called: {folder_path}")

    if not os.path.exists(folder_path):
        dlog("❌ Folder path does not exist")
        return None

    for f in os.listdir(folder_path):
        dlog(f"Found file in folder: {f}")

        if f.lower().endswith(".svg"):
            dlog(f"✅ SVG file selected: {f}")
            return f

    dlog("❌ No SVG file found in folder")
    return None


# ------------------------------------------------
# STEP 4 — CALL LIFAFAY SYSTEM
# ------------------------------------------------

def send_to_lifafay(payload: dict):
    dlog("Sending payload to Lifafay system")
    dlog(f"Payload → {payload}")

    try:
        resp = requests.post(
            LIFAFAY_API_URL,
            json=payload,
            timeout=20
        )

        dlog(f"Lifafay HTTP status → {resp.status_code}")
        dlog(f"Lifafay response body → {resp.text}")

        return resp.ok

    except Exception as e:
        dlog(f"❌ Lifafay request failed: {e}")
        return False


# ------------------------------------------------
# MAIN ENTRY — PHASE 1
# ------------------------------------------------

def handle_design_reply(
    phone: str,
    customer_text: str,
    reply_caption: str,
    reply_whatsapp_id: str
):
    dlog("================================================")
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

    # 2️⃣ Find order folder
    folder = find_order_folder(phone, reply_caption)
    if not folder:
        dlog("❌ EXIT: Order folder not found")
        return False

    # 3️⃣ Find SVG
    svg_file = find_svg_from_folder(folder)
    if not svg_file:
        dlog("❌ EXIT: SVG file not found")
        return False

    # 4️⃣ Build payload
    payload = {
        "order_phone": phone,
        "folder_path": folder,
        "svg_file": svg_file,
        "action": {
            "type": "move_text_alignment",
            "target_text_id": "text2",
            "alignment": alignment
        },
        "reply_whatsapp_id": reply_whatsapp_id
    }

    # 5️⃣ Send to Lifafay
    success = send_to_lifafay(payload)

    if success:
        dlog("✅ Design edit request successfully sent to Lifafay")
    else:
        dlog("❌ Failed to send design edit request")

    dlog("handle_design_reply FINISHED")
    dlog("================================================")

    return success
