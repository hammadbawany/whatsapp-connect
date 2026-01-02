import re
import sys
import json
import requests

from app.plugins.dropbox_plugin import get_system_dropbox_client


# =====================================================
# CONFIG
# =====================================================

BASE_DROPBOX_FOLDER = "/1 daniyal/Auto/send to customer"

LIFAFAY_ENDPOINT = "https://lifafay.herokuapp.com/api/design/action"  # 🔁 CHANGE IF NEEDED
LIFAFAY_API_KEY = None  # optional, if you want auth later


# =====================================================
# LOGGING
# =====================================================

def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()


# =====================================================
# HELPERS — PHONE NORMALIZATION
# =====================================================

def normalize_digits(s: str):
    if not s:
        return ""
    return re.sub(r"\D", "", s)


def matches_phone(folder_name: str, phone: str):
    """
    Matches phone against folder name.
    Supports:
    - +923...
    - 923...
    - 03...
    - multiple numbers in folder
    """
    folder_normalized = normalize_digits(folder_name)

    if not folder_normalized or not phone:
        return False

    # last 10 digits are usually stable
    phone_tail = phone[-10:]

    return phone_tail in folder_normalized


# =====================================================
# STEP 1 — ALIGNMENT INTENT (PHASE 1 ONLY)
# =====================================================

def detect_alignment_intent(text: str):
    dlog(f"detect_alignment_intent called with text='{text}'")

    t = text.lower()

    if any(k in t for k in [
        "center", "centre", "beech", "center kar", "center karo"
    ]):
        dlog("✅ Alignment detected: center")
        return "center"

    if any(k in t for k in [
        "right", "dayen", "right side"
    ]):
        dlog("✅ Alignment detected: right")
        return "right"

    if any(k in t for k in [
        "left", "baen", "left side"
    ]):
        dlog("✅ Alignment detected: left")
        return "left"

    dlog("❌ No alignment intent detected")
    return None


# =====================================================
# STEP 2 — FIND ORDER FOLDER (DROPBOX)
# =====================================================

def find_order_folder_by_phone(dbx, base_folder: str, phone: str):
    dlog("find_order_folder_by_phone called")
    dlog(f"phone={phone}")

    phone_norm = normalize_digits(phone)
    dlog(f"normalized phone={phone_norm}")

    matches = []

    try:
        result = dbx.files_list_folder(base_folder)
        entries = result.entries

        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)

    except Exception as e:
        dlog(f"❌ Dropbox list failed: {e}")
        return None

    for entry in entries:
        if entry.__class__.__name__ != "FolderMetadata":
            continue

        folder_name = entry.name

        if matches_phone(folder_name, phone_norm):
            matches.append(entry.path_lower)

    dlog(f"Matched folders count = {len(matches)}")
    dlog(f"Matched folders = {matches}")

    if not matches:
        dlog("❌ No folder found for this phone")
        return None

    if len(matches) == 1:
        return matches[0]

    # Prefer exact phone match if multiple
    for m in matches:
        if phone_norm in normalize_digits(m):
            return m

    matches.sort()
    return matches[0]


# =====================================================
# STEP 3 — FIND SVG FILE (DROPBOX)
# =====================================================

def find_svg_from_folder(dbx, folder_path: str):
    dlog(f"find_svg_from_folder called for {folder_path}")

    try:
        result = dbx.files_list_folder(folder_path)
        entries = result.entries

        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)

    except Exception as e:
        dlog(f"❌ Dropbox list SVG failed: {e}")
        return None

    for entry in entries:
        if entry.__class__.__name__ == "FileMetadata" and entry.name.lower().endswith(".svg"):
            dlog(f"✅ SVG found: {entry.path_lower}")
            return entry.path_lower

    dlog("❌ No SVG file found in folder")
    return None


# =====================================================
# MAIN ENTRY — PHASE 1
# =====================================================

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

    # STEP 1 — intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ EXIT: No alignment intent")
        return False

    dlog(f"Alignment intent → {alignment}")

    # STEP 2 — Dropbox client
    dbx = get_system_dropbox_client()

    # STEP 3 — find folder
    folder_path = find_order_folder_by_phone(
        dbx=dbx,
        base_folder=BASE_DROPBOX_FOLDER,
        phone=phone
    )

    if not folder_path:
        dlog("❌ EXIT: Order folder not found")
        return False

    dlog(f"Matched folder → {folder_path}")

    # STEP 4 — find SVG
    svg_path = find_svg_from_folder(dbx, folder_path)
    if not svg_path:
        dlog("❌ EXIT: SVG not found")
        return False

    # STEP 5 — SEND TO LIFAFAY (NO SVG EDIT HERE)
    payload = {
        "folder_path": folder_path,
        "svg_path": svg_path,
        "action": "move_text",
        "alignment": alignment,
        "phone": phone,
        "reply_whatsapp_id": reply_whatsapp_id,
        "source": "whatsapp"
    }

    dlog(f"🚀 Sending payload to Lifafay → {json.dumps(payload)}")

    headers = {"Content-Type": "application/json"}
    if LIFAFAY_API_KEY:
        headers["Authorization"] = f"Bearer {LIFAFAY_API_KEY}"

    try:
        resp = requests.post(
            LIFAFAY_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=15
        )

        dlog(f"📡 Lifafay response status={resp.status_code}")
        dlog(f"📡 Lifafay response body={resp.text}")

    except Exception as e:
        dlog(f"❌ Lifafay call failed: {e}")
        return False

    dlog("✅ Design reply handed off to Lifafay successfully")
    return True
