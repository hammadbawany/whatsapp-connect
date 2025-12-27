import os
import re
import time
import json
import requests
import dropbox
import mimetypes
from datetime import datetime
from flask import Blueprint, jsonify, session, request
from db import get_conn
from psycopg2.extras import RealDictCursor

# Define Blueprint
design_sender_bp = Blueprint("design_sender", __name__)

# --- CONFIGURATION ---
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
# Target WABA ID from your app.py settings
TARGET_WABA_ID = "1628402398537645"

TARGET_PATHS = [
    "/1 daniyal/Auto"
]

IGNORED_FOLDERS = [
    "instagram", "no reply", "confirm", "file issues",
    "cancelled orders", "correction done", "faraz corrections", "send to customer"
]

# Destination for moved folders
MOVE_DESTINATION_BASE = "/1 daniyal/Auto/send to customer"

# ====================================================
# 1. HELPERS: DB, DROPBOX & FILE OPS
# ====================================================

def init_log_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS design_sent_log (
            folder_name TEXT PRIMARY KEY,
            phone_number TEXT,
            sent_at TIMESTAMP DEFAULT NOW(),
            file_name TEXT,
            status TEXT
        );
    """)
    conn.commit()
    cur.close(); conn.close()

def get_dropbox_client(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT access_token, refresh_token FROM dropbox_accounts WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row: raise Exception("Dropbox not connected")
    if isinstance(row, dict): at, rt = row['access_token'], row['refresh_token']
    else: at, rt = row[0], row[1]

    try:
        dbx = dropbox.Dropbox(at)
        dbx.users_get_current_account()
        return dbx
    except:
        # Refresh Logic
        u = "https://api.dropboxapi.com/oauth2/token"
        d = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": APP_KEY, "client_secret": APP_SECRET}
        r = requests.post(u, data=d).json()
        new_at = r.get("access_token")
        if not new_at: raise Exception("Failed to refresh Dropbox token")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE dropbox_accounts SET access_token = %s WHERE user_id = %s", (new_at, user_id))
        conn.commit(); cur.close(); conn.close()
        return dropbox.Dropbox(new_at)

def mark_design_as_sent(folder_name, phone, file_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO design_sent_log (folder_name, phone_number, file_name, status)
        VALUES (%s, %s, %s, 'sent')
        ON CONFLICT (folder_name) DO NOTHING
    """, (folder_name, phone, file_name))
    conn.commit()
    cur.close(); conn.close()

def move_folder_after_sending(dbx, current_full_path, folder_name):
    """
    Moves the folder to the 'send to customer' directory after processing.
    """
    target_path = f"{MOVE_DESTINATION_BASE}/{folder_name}"

    # 1. Check if source and target are the same
    if current_full_path.lower().rstrip('/') == target_path.lower().rstrip('/'):
        print(f"[MOVE] Folder already in destination: {folder_name}")
        return True

    try:
        print(f"[MOVE] Moving '{current_full_path}' -> '{target_path}'")
        dbx.files_move_v2(from_path=current_full_path, to_path=target_path)
        return True
    except dropbox.exceptions.ApiError as e:
        # Handle case where folder already exists at destination
        if isinstance(e.error, dropbox.files.RelocationError) and e.error.is_to() and e.error.get_to().is_conflict():
            print(f"[MOVE ERROR] Destination already exists: {folder_name}")
            return False
        print(f"[MOVE ERROR] Failed to move folder: {e}")
        return False

# ====================================================
# 2. PARSING LOGIC
# ====================================================

def parse_folder_name(folder_name):
    # Extract Phones
    found_phones = []
    # Space separated (0321 1234567)
    for m in re.finditer(r'(?:^|[\s\-_])((?:0092|92|0)?3\d{2})\s+(\d{7})(?:$|[\s\-_])', folder_name):
        found_phones.append(m.group(1) + m.group(2))
    # Contiguous
    for m in re.finditer(r'(?:^|[\s\-_])(\+|00)?(\d{10,15})(?:$|[\s\-_])', folder_name):
        found_phones.append(m.group(2))

    normalized_phones = []
    for raw in found_phones:
        if raw.startswith("03") and len(raw) == 11: normalized_phones.append("92" + raw[1:])
        elif raw.startswith("3") and len(raw) == 10: normalized_phones.append("92" + raw)
        elif raw.startswith("00"): normalized_phones.append(raw[2:])
        elif raw.startswith("0"): normalized_phones.append(raw[1:])
        else: normalized_phones.append(raw)

    final_phones = list(set(normalized_phones))

    # Extract Code
    order_code = None
    code_match = re.search(r'---\s*(\d{5})\s*---', folder_name)
    if code_match: order_code = code_match.group(1)

    # Extract Source
    parts = re.split(r'\s*-{2,3}\s*|\s+--\s+', folder_name)
    parts = [p.strip().lower() for p in parts if p.strip()]

    source = "Unknown"
    keywords = ['website', 'web', 'whatsapp', 'whats app', 'wa', 'insta', 'instagram', 'facebook', 'fb']
    for part in parts:
        if any(k in part for k in keywords):
            if 'whats' in part or 'wa' == part: source = 'WhatsApp'
            elif 'web' in part: source = 'Website'
            elif 'insta' in part: source = 'Instagram'
            else: source = part.title()
            break

    return {"folder_name": folder_name, "phones": final_phones, "order_code": order_code, "source": source}

# ====================================================
# 3. WHATSAPP SENDER (MEDIA & TEXT)
# ====================================================

def normalize_phone(phone):
    if not phone: return ""
    p = re.sub(r"\D", "", str(phone))
    if p.startswith("03") and len(p) == 11: return "92" + p[1:]
    return p

def get_whatsapp_creds():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Get Account
    cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    acc = cur.fetchone()

    if not acc:
        cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        acc = cur.fetchone()

    cur.close(); conn.close()

    if not acc: raise Exception("No WhatsApp Account found in Database")
    return acc

def send_file_via_meta_and_db(phone, file_bytes, filename, mime_type, caption):
    """Sends media and logs to DB."""
    clean_phone = normalize_phone(phone)
    acc = get_whatsapp_creds()

    # 1. Upload
    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"
    headers = {"Authorization": f"Bearer {acc['access_token']}"}
    files = {'file': (filename, file_bytes, mime_type), 'messaging_product': (None, 'whatsapp')}

    r = requests.post(url, headers=headers, files=files)
    if r.status_code != 200: raise Exception(f"Meta Upload Failed: {r.text}")
    media_id = r.json().get('id')

    # 2. Send Message
    msg_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    msg_type = "image" if "image" in mime_type else "document"

    payload = {"messaging_product": "whatsapp", "to": clean_phone, "type": msg_type}
    if msg_type == "image": payload["image"] = {"id": media_id, "caption": caption}
    else: payload["document"] = {"id": media_id, "caption": caption, "filename": filename}

    # FIXED QUOTES HERE vvv
    r2 = requests.post(msg_url, headers={'Authorization': f"Bearer {acc['access_token']}", 'Content-Type': 'application/json'}, json=payload)
    resp_json = r2.json()

    if r2.status_code not in [200, 201]: raise Exception(f"Message Send Failed: {r2.text}")
    wa_id = resp_json.get('messages', [{}])[0].get('id')

    # 3. DB Insert
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (whatsapp_account_id, user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp)
        VALUES (%s, %s, 'agent', %s, %s, %s, %s, 'sent', NOW())
    """, (acc['id'], clean_phone, msg_type, media_id, caption, wa_id))
    conn.commit(); cur.close(); conn.close()
    return True

def send_text_via_meta_and_db(phone, text):
    """Sends text message and logs to DB."""
    clean_phone = normalize_phone(phone)
    acc = get_whatsapp_creds()

    msg_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": text}
    }

    # FIXED QUOTES HERE vvv
    r = requests.post(msg_url, headers={'Authorization': f"Bearer {acc['access_token']}", 'Content-Type': 'application/json'}, json=payload)
    resp_json = r.json()

    if r.status_code not in [200, 201]: raise Exception(f"Text Send Failed: {r.text}")
    wa_id = resp_json.get('messages', [{}])[0].get('id')

    # DB Insert
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
        VALUES (%s, %s, 'agent', %s, %s, 'sent', NOW())
    """, (acc['id'], clean_phone, text, wa_id))
    conn.commit(); cur.close(); conn.close()
    return True

# ====================================================
# 4. API ROUTES
# ====================================================

@design_sender_bp.route("/automation/preview")
def preview_automation():
    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)

    # 1. Scan Folders
    all_folders = []
    for path in TARGET_PATHS:
        try:
            res = dbx.files_list_folder(path)
            all_folders.extend(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                all_folders.extend(res.entries)
        except: pass

    folder_names = list(set([e.name for e in all_folders if isinstance(e, dropbox.files.FolderMetadata)]))

    # 2. Parse & Check DB
    parsed_list = []
    all_phones = set()

    for name in folder_names:
        if any(ign in name.lower() for ign in IGNORED_FOLDERS): continue
        d = parse_folder_name(name)
        if d['phones']:
            parsed_list.append(d)
            for p in d['phones']: all_phones.add(p[-10:])

    responded_set = set()
    if all_phones:
        conn = get_conn(); cur = conn.cursor()
        chk = list(all_phones)
        fmt = ','.join(['%s'] * len(chk))
        cur.execute(f"SELECT DISTINCT RIGHT(user_phone, 10) FROM messages WHERE sender='customer' AND RIGHT(user_phone, 10) IN ({fmt})", tuple(chk))
        for r in cur.fetchall(): responded_set.add(r[0] if not isinstance(r,dict) else r['short_phone' if 'short_phone' in r else 0])

        cur.execute("SELECT folder_name FROM design_sent_log")
        sent_set = {r[0] if not isinstance(r,dict) else r['folder_name'] for r in cur.fetchall()}
        cur.close(); conn.close()
    else: sent_set = set()

    # 3. Filter Eligible
    eligible = []
    for item in parsed_list:
        if item['folder_name'] in sent_set: continue

        active = item['phones'][0]
        replied = False
        for p in item['phones']:
            if p[-10:] in responded_set:
                active = p
                replied = True
                break

        is_wa = "whatsapp" in item['source'].lower()
        if is_wa or replied:
            item['active_phone'] = active
            eligible.append(item)

    return jsonify({"count": len(eligible), "folders": eligible})


@design_sender_bp.route("/run_auto_design_delivery", methods=['POST'])
def run_auto_design_delivery():
    """BATCH EXECUTION"""
    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    folders = data.get('folders', [])
    if not folders: return jsonify({"sent_count": 0, "errors": ["No folders"]})

    init_log_table()
    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)

    sent_count = 0
    errors = []

    for item in folders:
        folder = item['folder_name']
        phone = item.get('phone') or item.get('active_phone')

        # Check DB first
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM design_sent_log WHERE folder_name=%s", (folder,))
        if cur.fetchone():
            cur.close(); conn.close()
            continue
        cur.close(); conn.close()

        # Find Path
        full_path = item.get('full_path')
        if not full_path:
            for base in TARGET_PATHS:
                try:
                    t = f"{base}/{folder}"
                    dbx.files_get_metadata(t); full_path = t; break
                except: continue

        if not full_path:
            errors.append(f"{folder}: Not found")
            continue

        # Scan for PNGs
        try:
            entries = dbx.files_list_folder(full_path).entries
            pngs = [e for e in entries if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith('.png')]
        except Exception as e:
            errors.append(f"{folder}: Scan error - {str(e)}")
            continue

        if not pngs:
            errors.append(f"{folder}: No PNGs")
            continue

        # Send All PNGs
        folder_success = False
        for i, f in enumerate(pngs):
            if i > 0: time.sleep(15)
            try:
                _, res = dbx.files_download(f.path_lower)
                caption = os.path.splitext(f.name)[0]
                send_file_via_meta_and_db(phone, res.content, f.name, "image/png", caption)
                folder_success = True
            except Exception as e:
                errors.append(f"{folder}/{f.name}: {str(e)}")
        time.sleep(11)
        if folder_success:
            # ✅ SEND CONFIRMATION TEXT
            try:
                msg = "Please confirm text and design.\nNo changes will be made after confirmation"
                send_text_via_meta_and_db(phone, msg)
            except Exception as e:
                print(f"Failed to send confirmation text: {e}")

            sent_count += 1
            mark_design_as_sent(folder, phone, f"{len(pngs)} files")
            # ✅ MOVE FOLDER
            move_folder_after_sending(dbx, full_path, folder)

    return jsonify({"status": "success", "sent_count": sent_count, "errors": errors})


@design_sender_bp.route("/manual_send_design", methods=['POST'])
def manual_send_design():
    """MANUAL BUTTON"""
    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    folder = data.get('folder_name')
    phone = data.get('phone')
    path = data.get('full_path')

    if not folder or not phone: return jsonify({"error": "Missing data"}), 400

    init_log_table()

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM design_sent_log WHERE folder_name = %s", (folder,))
    exists = cur.fetchone()
    cur.close(); conn.close()

    if exists:
        return jsonify({"error": "⚠️ Design already marked as sent in Database!"}), 409

    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)

    sent_count = 0
    errors = []

    try:
        entries = []
        res = dbx.files_list_folder(path)
        entries.extend(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            entries.extend(res.entries)

        pngs = [e for e in entries if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith('.png')]

        if not pngs: return jsonify({"error": "No .png files found"}), 404

        for i, f in enumerate(pngs):
            if i > 0: time.sleep(15)

            try:
                _, res = dbx.files_download(f.path_lower)
                caption = os.path.splitext(f.name)[0]

                # 🔥 USE THE DB-SYNCED SENDER
                send_file_via_meta_and_db(phone, res.content, f.name, "image/png", caption)

                sent_count += 1
            except Exception as e:
                errors.append(f"{f.name}: {str(e)}")

        if sent_count > 0:
            # ✅ SEND CONFIRMATION TEXT
            try:
                msg = "Please confirm text and design.\nNo changes will be made after confirmation"
                send_text_via_meta_and_db(phone, msg)
            except Exception as e:
                print(f"Failed to send confirmation text: {e}")

            mark_design_as_sent(folder, phone, f"{sent_count} PNGs")

            # ✅ MOVE FOLDER
            move_folder_after_sending(dbx, path, folder)

        return jsonify({"status": "success", "sent_count": sent_count, "total_found": len(pngs), "errors": errors})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
