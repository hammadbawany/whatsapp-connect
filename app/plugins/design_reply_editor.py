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
    Horizontal + Vertical (limited).
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
    # VERTICAL FIRST (BOTTOM)
    # ----------------------------
    is_bottom = any(k in t for k in [
        "bottom", "neeche", "neechay", "nechy",
        "lower", "down", "neechey"
    ])

    # ----------------------------
    # HORIZONTAL
    # ----------------------------
    is_center = any(k in t for k in [
        "center", "centre", "beech",
        "center mein", "center mn"
    ])

    is_left = any(k in t for k in [
        "left", "left side", "baen"
    ])

    is_right = any(k in t for k in [
        "right", "right side", "dayen"
    ])

    # ----------------------------
    # COMBINATIONS
    # ----------------------------
    if is_bottom:
        if is_left:
            return "bottom_left"
        if is_right:
            return "bottom_right"
        return "bottom_center"

    # ----------------------------
    # FALLBACK (HORIZONTAL ONLY)
    # ----------------------------
    if is_center:
        return "center"

    if is_left:
        return "left"

    if is_right:
        return "right"

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

    def process_entries(entries):
        for entry in entries:
            if not entry.name:
                continue

            name_lower = entry.name.lower()

            # ⛔️ Block confirm folder
            if name_lower == "confirm" or name_lower.endswith("/confirm"):
                continue

            # Must contain ---
            if "---" not in entry.name:
                continue

            folder_digits = normalize_digits(entry.name)

            if target_digits in folder_digits:
                dlog(f"✅ MATCH FOUND: {entry.path_display}")
                matched.append(entry.path_display)

    # 🔁 SEARCH EACH BASE FOLDER
    for base_folder in BASE_DROPBOX_FOLDERS:
        dlog(f"📂 Searching in: {base_folder}")

        try:
            res = dbx.files_list_folder(base_folder)
        except Exception as e:
            dlog(f"⚠️ Cannot access {base_folder}: {e}")
            continue

        process_entries(res.entries)

        while res.has_more:
            try:
                res = dbx.files_list_folder_continue(res.cursor)
                process_entries(res.entries)
            except Exception as e:
                dlog(f"⚠️ Pagination error in {base_folder}: {e}")
                break

    if not matched:
        dlog(f"❌ No folder found containing {target_digits}")
        return None

    # Prefer folder starting with phone number
    selected_path = matched[0]
    if len(matched) > 1:
        dlog(f"⚠️ Multiple matches found:")
        for m in matched:
            dlog(f"   - {m}")
            folder_name = m.split("/")[-1]
            if target_digits in normalize_digits(folder_name.split('---')[0]):
                selected_path = m
                break

    # Final safety check
    if selected_path.lower().endswith("/confirm"):
        dlog("❌ ERROR: Selected path is 'confirm'. Aborting.")
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


def detect_confirmation_intent(text: str):
    if not text:
        return False

    t = text.lower().strip()

    strong_confirm = [
        "confirm", "confirmed", "approved", "final", "ok confirmed",
        "yes confirmed", "go ahead", "print it", "proceed"
    ]

    soft_confirm = [
        "ok", "okay", "done", "perfect", "looks good", "this is fine",
        "alright", "sure", "jee", "ji", "sahi hai", "theek hai"
    ]

    emojis = ["👍", "👌"]

    # Strong keywords
    if any(k in t for k in strong_confirm):
        return True

    # Emoji-only confirmations
    if any(e in t for e in emojis) and len(t) <= 3:
        return True

    # Soft confirmations (only if message is short)
    if any(k == t or t.startswith(k) for k in soft_confirm) and len(t) <= 15:
        return True

    return False
