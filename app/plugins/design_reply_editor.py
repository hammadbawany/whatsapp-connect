import os
import re
import sys
import requests
from app.plugins.dropbox_plugin import get_system_dropbox_client

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
def find_order_folder(dbx, phone: str, caption: str):
    dlog("Searching order folder in Dropbox")

    # Try extracting order code
    match = re.search(r"\b\d{4,6}\b", caption)
    order_code = match.group(0) if match else None

    dlog(f"Extracted order_code={order_code}")

    res = dbx.files_list_folder(DROPBOX_BASE_FOLDER)

    for entry in res.entries:
        if not entry.name.startswith(phone):
            continue

        if order_code and order_code not in entry.name:
            continue

        folder_path = f"{DROPBOX_BASE_FOLDER}/{entry.name}"
        dlog(f"Matched Dropbox folder → {folder_path}")
        return folder_path

    return None


# -----------------------------
# STEP 3 — FIND SVG FILE (DROPBOX)
# -----------------------------
def find_svg_file(dbx, folder_path: str):
    res = dbx.files_list_folder(folder_path)

    for entry in res.entries:
        if entry.name.lower().endswith(".svg"):
            svg_path = f"{folder_path}/{entry.name}"
            dlog(f"Matched SVG → {svg_path}")
            return svg_path

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
    dlog("==============================================")
    dlog("handle_design_reply STARTED")

    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ No alignment intent")
        return False

    dlog(f"Alignment intent → {alignment}")

    dbx = get_system_dropbox_client()
    if not dbx:
        dlog("❌ Dropbox client not available")
        return False

    folder = find_order_folder(dbx, phone, reply_caption)
    if not folder:
        dlog("❌ Order folder not found")
        return False

    svg_path = find_svg_file(dbx, folder)
    if not svg_path:
        dlog("❌ SVG file not found")
        return False

    # 🚀 PHASE-1 OUTPUT (NO SVG EDIT HERE)
    dlog("✅ READY FOR LIFAFAY SYSTEM")
    dlog(f"SVG_PATH={svg_path}")
    dlog(f"ACTION=move_text")
    dlog(f"ALIGNMENT={alignment}")

    # 🔜 NEXT PHASE:
    # POST svg_path + alignment to Lifafay API

    return True
