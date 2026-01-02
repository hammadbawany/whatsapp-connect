import re
import sys

from app.plugins.dropbox_plugin import get_system_dropbox_client

# =========================================================
# CONFIG
# =========================================================

# Dropbox base folder where designs live
BASE_DROPBOX_FOLDER = "/1 daniyal/Auto/send to customer"

# =========================================================
# LOGGING
# =========================================================

def dlog(msg):
    print(f"[DESIGN-REPLY] {msg}", file=sys.stdout)
    sys.stdout.flush()


# =========================================================
# STEP 1 — DETECT ALIGNMENT INTENT (WHATSAPP SIDE ONLY)
# =========================================================

def detect_alignment_intent(text: str):
    dlog(f"detect_alignment_intent called with text='{text}'")

    t = text.lower()

    if any(k in t for k in ["center", "centre", "beech", "center kar", "center karo"]):
        dlog("✅ Alignment detected: center")
        return "center"

    if any(k in t for k in ["right", "right side", "dayen", "dayin"]):
        dlog("✅ Alignment detected: right")
        return "right"

    if any(k in t for k in ["left", "left side", "baen", "baein"]):
        dlog("✅ Alignment detected: left")
        return "left"

    dlog("❌ No alignment intent detected")
    return None


# =========================================================
# STEP 2 — FIND ORDER FOLDER FROM DROPBOX
# =========================================================

def find_order_folder(dbx, phone: str, caption: str):
    dlog("find_order_folder called")
    dlog(f"phone={phone}")
    dlog(f"caption='{caption}'")

    try:
        result = dbx.files_list_folder(BASE_DROPBOX_FOLDER)
    except Exception as e:
        dlog(f"❌ Dropbox list folder failed: {e}")
        return None

    matched_folders = []

    for entry in result.entries:
        if entry.__class__.__name__ != "FolderMetadata":
            continue

        folder_name = entry.name

        if phone in folder_name:
            matched_folders.append(folder_name)

    dlog(f"Matched folders count = {len(matched_folders)}")
    dlog(f"Matched folders = {matched_folders}")

    # ✅ Phase-1 rule: only proceed if exactly ONE folder
    if len(matched_folders) == 1:
        final_path = f"{BASE_DROPBOX_FOLDER}/{matched_folders[0]}"
        dlog(f"✅ Using folder: {final_path}")
        return final_path

    if len(matched_folders) == 0:
        dlog("❌ No folder found for this phone")
        return None

    dlog("⚠️ Multiple folders found — ambiguous (Phase-2 case)")
    return None


# =========================================================
# STEP 3 — FIND SVG FILE INSIDE ORDER FOLDER
# =========================================================

def find_svg_from_folder(dbx, folder_path: str):
    dlog(f"find_svg_from_folder called for {folder_path}")

    try:
        result = dbx.files_list_folder(folder_path)
    except Exception as e:
        dlog(f"❌ Failed to list folder {folder_path}: {e}")
        return None

    for entry in result.entries:
        if entry.__class__.__name__ != "FileMetadata":
            continue

        if entry.name.lower().endswith(".svg"):
            svg_path = f"{folder_path}/{entry.name}"
            dlog(f"✅ SVG found: {svg_path}")
            return svg_path

    dlog("❌ No SVG file found in folder")
    return None


# =========================================================
# MAIN ENTRY — PHASE 1 ONLY (NO SVG EDITING HERE)
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

    # 1️⃣ Detect alignment intent
    alignment = detect_alignment_intent(customer_text)
    if not alignment:
        dlog("❌ EXIT: No alignment intent")
        return False

    dlog(f"Alignment intent → {alignment}")

    # 2️⃣ Get Dropbox client (EXISTING AUTH)
    dbx = get_system_dropbox_client()
    if not dbx:
        dlog("❌ EXIT: Dropbox client not available")
        return False

    # 3️⃣ Find order folder
    folder_path = find_order_folder(dbx, phone, reply_caption)
    if not folder_path:
        dlog("❌ EXIT: Order folder not found")
        return False

    # 4️⃣ Find SVG file
    svg_path = find_svg_from_folder(dbx, folder_path)
    if not svg_path:
        dlog("❌ EXIT: SVG not found")
        return False

    # =====================================================
    # 🚀 PHASE 1 COMPLETE (HANDOFF POINT)
    # =====================================================
    # At this point we ONLY prepare data.
    # Actual SVG editing will be done by Lifafay system
    # via API in Phase 2.

    dlog("✅ PHASE 1 SUCCESS")
    dlog(f"READY FOR LIFAFAY → folder={folder_path}")
    dlog(f"READY FOR LIFAFAY → svg={svg_path}")
    dlog(f"READY FOR LIFAFAY → action=move_text")
    dlog(f"READY FOR LIFAFAY → alignment={alignment}")

    payload = {
    "folder_path": folder_path,
    "svg_path": svg_path,
    "action": "move_text",
    "alignment": alignment,
    "source": "whatsapp",
    "phone": phone,
    "reply_whatsapp_id": reply_whatsapp_id
    }

    dlog("📤 Sending payload to Lifafay")
    dlog(payload)

    try:
        import requests
        LIFAFAY_ENDPOINT = os.getenv("LIFAFAY_EDIT_ENDPOINT")

        resp = requests.post(
            LIFAFAY_ENDPOINT,
            json=payload,
            timeout=20
        )

        dlog(f"📥 Lifafay response status={resp.status_code}")
        dlog(resp.text)

        return True

    except Exception as e:
        dlog(f"❌ Lifafay call failed: {e}")
        return False
