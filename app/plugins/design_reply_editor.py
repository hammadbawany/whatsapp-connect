import re
import sys
import json
import os

# ===============================
# IMPORTS
# ===============================

try:
    # ✅ Correct Path: app.plugins.lifafay_client
    from app.plugins.lifafay_client import send_to_lifafay
except ImportError:
    # Fallback: Try root if moved, otherwise define locally to prevent crashes
    try:
        from lifafay_client import send_to_lifafay
    except ImportError:
        import requests
        def send_to_lifafay(payload):
            print("[FALLBACK] Sending to Lifafay (Client import failed)...", file=sys.stdout)
            url = "https://lifafay.herokuapp.com/api/design/action"
            try:
                res = requests.post(url, json=payload, timeout=20)
                print(f"[FALLBACK] Status: {res.status_code}", file=sys.stdout)
                return res.status_code == 200
            except Exception as e:
                print(f"[FALLBACK] Error: {e}", file=sys.stdout)
                return False

from app.plugins.dropbox_plugin import get_system_dropbox_client

# ===============================
# CONFIG
# ===============================

BASE_DROPBOX_FOLDERS = [
    "/1 daniyal/Auto/send to customer",
    "/1 daniyal/Auto/send to customer/Correction done",
    "/1 daniyal/Auto/send to customer/Edited by AI",
]

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
    """
    Phase-1 intent detection:
    Detect ONLY placement / movement of text.
    """
    if not text:
        return None

    t = text.lower()

    # Must contain a movement / placement verb
    move_verbs = [
        "move", "shift", "place", "write",
        "kar do", "kar dein", "kar den",
        "laga do", "laga dein",
        "rakh do", "rakh dein"
    ]

    if not any(v in t for v in move_verbs):
        return None

    # ----------------------------
    # VERTICAL
    # ----------------------------
    is_bottom = any(k in t for k in ["bottom", "neeche", "neechay", "nechy", "lower", "down"])
    is_top = any(k in t for k in ["top", "upar", "upper", "upr"])

    # ----------------------------
    # HORIZONTAL
    # ----------------------------
    is_center = any(k in t for k in ["center", "centre", "beech"])
    is_left = any(k in t for k in ["left", "baen", "ultey"])
    is_right = any(k in t for k in ["right", "dayen", "seedhay"])

    # ----------------------------
    # COMBINATIONS
    # ----------------------------
    if is_bottom:
        if is_left: return "bottom_left"
        if is_right: return "bottom_right"
        return "bottom_center"

    if is_top:
        if is_left: return "top_left"
        if is_right: return "top_right"
        return "top_center"

    # ----------------------------
    # HORIZONTAL ONLY
    # ----------------------------
    if is_center: return "center"
    if is_left: return "left"
    if is_right: return "right"

    return None

def detect_confirmation_intent(text: str):
    if not text: return False
    t = text.lower().strip()

    strong_confirm = ["confirm", "confirmed", "approved", "final", "ok confirmed", "proceed", "print"]
    soft_confirm = ["ok", "done", "perfect", "good", "sahi", "theek"]
    emojis = ["👍", "👌", "✅"]

    if any(k in t for k in strong_confirm): return True
    if any(e in t for e in emojis) and len(t) <= 5: return True
    if any(k == t or t.startswith(k) for k in soft_confirm) and len(t) <= 15: return True

    return False

# ===============================
# STEP 2 — FIND ORDER FOLDER
# ===============================

def normalize_digits(s: str):
    return re.sub(r"\D", "", s or "")

def find_order_folder(dbx, phone: str):
    dlog(f"🔍 Searching for folder for Phone: {phone}")

    target_digits = normalize_digits(phone)
    if not target_digits or len(target_digits) < 5:
        dlog("❌ Phone number too short or invalid.")
        return None

    matched = []

    def process_entries(entries):
        for entry in entries:
            if not entry.name: continue

            # Skip confirm/system folders
            name_lower = entry.name.lower()
            if name_lower == "confirm" or name_lower.endswith("/confirm"): continue
            if "---" not in entry.name: continue

            folder_digits = normalize_digits(entry.name)
            if target_digits in folder_digits:
                dlog(f"✅ MATCH FOUND: {entry.path_display}")
                matched.append(entry.path_display)

    # Search configured base folders
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

    # Logic: If multiple matches, prefer the one where phone is at the start
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

    # Use the imported function
    success = send_to_lifafay(payload)

    if success:
        dlog("✅ Successfully handed off to Lifafay")
    else:
        dlog("❌ Failed to hand off to Lifafay")

    return success
