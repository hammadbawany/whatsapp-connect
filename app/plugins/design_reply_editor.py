import re
import sys
import json

# ===============================
# IMPORTS
# ===============================
try:
    from lifafay_client import send_to_lifafay
except ImportError:
    # Fallback if lifafay_client.py is missing
    import requests
    def send_to_lifafay(payload):
        print("[FALLBACK] Sending to Lifafay (Client missing)...")
        # Ensure we have the URL
        url = "https://lifafay.herokuapp.com/api/design/action"
        try:
            res = requests.post(url, json=payload, timeout=15)
            return res.status_code == 200
        except Exception as e:
            print(f"Error: {e}")
            return False

from app.plugins.dropbox_plugin import get_system_dropbox_client

# ===============================
# CONFIG
# ===============================

BASE_DROPBOX_FOLDER = "/1 daniyal/Auto/send to customer"

# ===============================
# LOGGING
# ===============================

def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()

# ===============================
# STEP 1 — INTENT
# ===============================

def detect_alignment_intent(text: str):
    t = text.lower()
    if any(k in t for k in ["center", "centre", "beech", "center karo", "center kar"]):
        return "center"
    if any(k in t for k in ["right", "dayen", "right side"]):
        return "right"
    if any(k in t for k in ["left", "baen", "left side"]):
        return "left"
    return None

# ===============================
# STEP 2 — FIND ORDER FOLDER
# ===============================

def normalize_digits(s: str):
    """Removes non-digit characters."""
    return re.sub(r"\D", "", s or "")

def find_order_folder(dbx, phone: str):
    dlog(f"🔍 Searching for folder for Phone: {phone}")

    target_digits = normalize_digits(phone)
    if not target_digits or len(target_digits) < 5:
        dlog("❌ Phone number too short or invalid.")
        return None

    matched = []

    try:
        res = dbx.files_list_folder(BASE_DROPBOX_FOLDER)
    except Exception as e:
        dlog(f"❌ Dropbox list failed: {e}")
        return None

    def process_entries(entries):
        for entry in entries:
            # Basic checks
            if not entry.name:
                continue

            # Use lower case for checking keywords
            name_lower = entry.name.lower()

            # ⛔️ CRITICAL: Block 'confirm' folder
            if name_lower == "confirm" or name_lower.endswith("/confirm"):
                # dlog(f"   [SKIP] Ignored restricted folder: {entry.name}")
                continue

            # ✅ RULE 1: Must contain '---'
            if "---" not in entry.name:
                # dlog(f"   [SKIP] Missing '---': {entry.name}")
                continue

            # ✅ RULE 2: Check phone number
            # We normalize the folder name to just digits to find the phone number
            # allowing for formats like "92-346..." or "92 346..."
            folder_digits = normalize_digits(entry.name)

            if target_digits in folder_digits:
                dlog(f"✅ MATCH FOUND: {entry.name}")
                matched.append(entry.path_display)
            else:
                # Optional: debug print for near-misses
                # dlog(f"   [SKIP] Phone mismatch: {entry.name}")
                pass

    # 1. Process first batch
    process_entries(res.entries)

    # 2. Pagination Loop
    while res.has_more:
        try:
            res = dbx.files_list_folder_continue(res.cursor)
            process_entries(res.entries)
        except Exception as e:
            dlog(f"⚠️ Dropbox pagination error: {e}")
            break

    # 3. Final Selection
    if not matched:
        dlog(f"❌ No folder found containing {target_digits}")
        return None

    # Prioritize folder starting with phone number if multiple found
    selected_path = matched[0]
    if len(matched) > 1:
        dlog(f"⚠️ Multiple matches: {matched}")
        for m in matched:
            if target_digits in normalize_digits(m.split('/')[-1].split('---')[0]):
                selected_path = m
                break

    # ⛔️ FINAL SAFETY CHECK
    if selected_path.lower().endswith("/confirm"):
        dlog("❌ ERROR: Selected path is 'confirm' despite checks. Aborting.")
        return None

    dlog(f"📂 Final Selected Folder: {selected_path}")
    return selected_path

# ===============================
# MAIN ENTRY
# ===============================

def handle_design_reply(
    phone: str,
    customer_text: str,
    reply_caption: str,
    reply_whatsapp_id: str
):
    dlog("=" * 46)
    dlog(f"🚀 handle_design_reply | Phone: {phone} | Caption: {reply_caption}")

    # 1️⃣ Detect intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ No alignment intent")
        return False

    # 2️⃣ Dropbox client
    try:
        dbx = get_system_dropbox_client()
    except Exception as e:
        dlog(f"❌ Dropbox auth failed: {e}")
        return False

    # 3️⃣ Find folder
    folder_path = find_order_folder(dbx, phone)
    if not folder_path:
        dlog("❌ EXIT: Could not find valid order folder.")
        return False

    # 4️⃣ Hand off to Lifafay
    payload = {
        "source": "whatsapp",
        "phone": phone,
        "reply_whatsapp_id": reply_whatsapp_id,
        "action": "move_text_alignment",
        "alignment": alignment,
        "dropbox_folder": folder_path,
        "caption": reply_caption
    }

    dlog("📡 Handing off to Lifafay...")
    success = send_to_lifafay(payload)

    return success
