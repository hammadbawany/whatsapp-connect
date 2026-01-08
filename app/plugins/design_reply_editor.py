import re
import sys
import json
import requests
import os

from app.plugins.dropbox_plugin import get_system_dropbox_client

# ===============================
# CONFIG
# ===============================

BASE_DROPBOX_FOLDERS = [
    "/1 daniyal/Auto/send to customer",
    "/1 daniyal/Auto/send to customer/Correction done",
    "/1 daniyal/Auto/send to customer/Edited by AI",
]

# 🔹 UPDATED: Added trailing slash to fix 405 Error
LIFAFAY_ENDPOINT = "https://lifafay.herokuapp.com/api/design/action/"

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
    if not text: return None
    t = text.lower()

    # 🔹 FIXED: Added 'should be', 'adjust', 'change', 'text', 'side'
    # If the user mentions "text" + "right", that is enough to signal intent.
    move_verbs = [
        "move", "shift", "place", "write", "change", "adjust", "should be",
        "kar do", "kar dein", "kar den", "laga do", "laga dein", "rakh do", "rakh dein", "kardo",
        "text", "side", "taraf"
    ]

    if not any(v in t for v in move_verbs): return None

    # VERTICAL
    is_bottom = any(k in t for k in ["bottom", "neeche", "neechay", "nechy", "lower", "down"])
    is_top = any(k in t for k in ["top", "upar", "upper", "upr"])

    # HORIZONTAL
    is_center = any(k in t for k in ["center", "centre", "beech"])
    is_left = any(k in t for k in ["left", "baen", "ultey"])
    is_right = any(k in t for k in ["right", "dayen", "seedhay"])

    # COMBINATIONS
    if is_bottom:
        if is_left: return "bottom_left"
        if is_right: return "bottom_right"
        return "bottom_center"
    if is_top:
        if is_left: return "top_left"
        if is_right: return "top_right"
        return "top_center"

    # HORIZONTAL ONLY
    if is_center: return "center"
    if is_left: return "bottom_left"
    if is_right: return "bottom_right"

    # VERTICAL ONLY (Implicit Center)
    if is_bottom: return "bottom_center"
    if is_top: return "top_center"

    return None
# ===============================
# STEP 2 — FIND ORDER FOLDER
# ===============================

def normalize_digits(s: str):
    return re.sub(r"\D", "", s or "")

def find_order_folder(dbx, phone: str):
    dlog(f"🔍 Searching for folder for Phone: {phone}")
    target_digits = normalize_digits(phone)
    if not target_digits or len(target_digits) < 5:
        dlog("❌ Phone number too short.")
        return None

    matched = []
    def process_entries(entries):
        for entry in entries:
            if not entry.name: continue
            name_lower = entry.name.lower()
            if name_lower == "confirm" or name_lower.endswith("/confirm"): continue
            if "---" not in entry.name: continue
            if target_digits in normalize_digits(entry.name):
                dlog(f"✅ MATCH FOUND: {entry.path_display}")
                matched.append(entry.path_display)

    for base_folder in BASE_DROPBOX_FOLDERS:
        dlog(f"📂 Searching in: {base_folder}")
        try:
            res = dbx.files_list_folder(base_folder)
            process_entries(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                process_entries(res.entries)
        except Exception as e:
            dlog(f"⚠️ Error accessing {base_folder}: {e}")

    if not matched:
        dlog(f"❌ No folder found containing {target_digits}")
        return None

    selected_path = matched[0]
    if len(matched) > 1:
        for m in matched:
            folder_name = m.split("/")[-1]
            if folder_name.replace("+", "").startswith(target_digits):
                selected_path = m
                break

    dlog(f"📂 Final Selected Folder: {selected_path}")
    return selected_path

# ===============================
# STEP 3 — SEND TO LIFAFAY
# ===============================

def send_to_lifafay(payload: dict):
    """
    Sends payload to Lifafay.
    Attempts with trailing slash first (to fix 405), then without.
    """
    url = LIFAFAY_ENDPOINT
    dlog(f"📡 Sending to Lifafay: {url}")

    try:
        resp = requests.post(url, json=payload, timeout=20)

        # If 405/404, try removing slash
        if resp.status_code in [404, 405, 308]:
            alt_url = url.rstrip("/") if url.endswith("/") else url + "/"
            dlog(f"⚠️ Got {resp.status_code}, trying alternate URL: {alt_url}")
            resp = requests.post(alt_url, json=payload, timeout=20)

        dlog(f"📥 Response Status: {resp.status_code}")

        if resp.status_code != 200:
            dlog(f"📥 Response Error: {resp.text}")
            return False

        return True

    except Exception as e:
        dlog(f"❌ API Request Failed: {e}")
        return False

# ===============================
# MAIN ENTRY
# ===============================

def handle_design_reply(phone: str, customer_text: str, reply_caption: str, reply_whatsapp_id: str):
    dlog("=" * 46)
    dlog(f"🚀 handle_design_reply | Phone: {phone} | Caption: {reply_caption}")

    # 1. Intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ No alignment intent")
        return False

    # 2. Dropbox
    try:
        dbx = get_system_dropbox_client()
    except Exception as e:
        dlog(f"❌ Dropbox auth failed: {e}")
        return False

    # 3. Folder
    folder_path = find_order_folder(dbx, phone)
    if not folder_path:
        dlog("❌ EXIT: No folder found.")
        return False

    # 4. API
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
        dlog("✅ Successfully handed off to Lifafay")
    else:
        dlog("❌ Failed to hand off to Lifafay")

    return success

def is_confirmation_text(text: str) -> bool:
    """
    Generic confirmation (non-image reply)
    """
    return is_design_confirmation(text)
