import os
import re
import time
import json
import requests
import dropbox
import mimetypes
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, session, request
from app.db import get_conn
from psycopg2.extras import RealDictCursor
import psycopg2
import logging

# Removed top-level import to prevent circular dependency
# from app import PENDING_DESIGN_CONFIRMATION

design_sender_bp = Blueprint("design_sender", __name__)

# --- CONFIGURATION ---
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
TARGET_WABA_ID = "1628402398537645"

TARGET_PATHS = [
    "/1 daniyal/Auto"
]

IGNORED_FOLDERS = [
    "instagram", "no reply", "confirm", "file issues",
    "cancelled orders", "correction done", "faraz corrections", "send to customer"
]

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
            status TEXT,
            sent_method TEXT DEFAULT 'manual'
        );
    """)
    conn.commit()
    cur.close(); conn.close()

def get_system_dropbox_client():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, access_token, refresh_token FROM dropbox_accounts LIMIT 1")
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row: return None
    if isinstance(row, dict): uid, at, rt = row['user_id'], row['access_token'], row['refresh_token']
    else: uid, at, rt = row[0], row[1], row[2]

    try:
        dbx = dropbox.Dropbox(at)
        dbx.users_get_current_account()
        return dbx
    except:
        try:
            u = "https://api.dropboxapi.com/oauth2/token"
            d = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": APP_KEY, "client_secret": APP_SECRET}
            r = requests.post(u, data=d).json()
            new_at = r.get("access_token")
            if not new_at: return None
            conn = get_conn(); cur = conn.cursor()
            cur.execute("UPDATE dropbox_accounts SET access_token = %s WHERE user_id = %s", (new_at, uid))
            conn.commit(); cur.close(); conn.close()
            return dropbox.Dropbox(new_at)
        except: return None

def get_dropbox_client(user_id):
    return get_system_dropbox_client()

def attempt_to_claim_folder(folder_name, phone, method='cron'):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM design_sent_log WHERE folder_name = %s", (folder_name,))
        if cur.fetchone():
            cur.close(); conn.close()
            return False

        cur.execute("""
            INSERT INTO design_sent_log (folder_name, phone_number, status, sent_method)
            VALUES (%s, %s, 'processing', %s)
        """, (folder_name, phone, method))

        conn.commit()
        cur.close(); conn.close()
        return True

    except psycopg2.IntegrityError:
        conn.rollback()
        cur.close(); conn.close()
        return False

    except Exception as e:
        print(f"❌ Locking Error: {e}")
        conn.rollback()
        cur.close(); conn.close()
        return False

def update_sent_status(folder_name, file_name, method, status='sent'):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        UPDATE design_sent_log
        SET status = %s, file_name = %s, sent_at = NOW(), sent_method = %s
        WHERE folder_name = %s
    """, (status, file_name, method, folder_name))
    conn.commit(); cur.close(); conn.close()

def release_lock_if_safe(folder_name, sent_any=False):
    conn = get_conn(); cur = conn.cursor()
    if sent_any:
        cur.execute("UPDATE design_sent_log SET status='partial_error' WHERE folder_name=%s", (folder_name,))
    else:
        cur.execute("DELETE FROM design_sent_log WHERE folder_name=%s", (folder_name,))
    conn.commit(); cur.close(); conn.close()

def move_folder_after_sending(dbx, current_full_path, folder_name):
    target_path = f"{MOVE_DESTINATION_BASE}/{folder_name}"
    if current_full_path.lower().rstrip('/') == target_path.lower().rstrip('/'): return True
    try:
        dbx.files_move_v2(from_path=current_full_path, to_path=target_path)
        print(f"✅ [MOVE] Moved to {target_path}")
        return True
    except Exception as e:
        print(f"❌ [MOVE FAILED] {e}")
        return False

# ====================================================
# 2. PARSING
# ====================================================

def parse_folder_name(folder_name):
    phones = []
    for m in re.finditer(r'(?:^|[\s\-_])((?:0092|92|0)?3\d{2})\s+(\d{7})(?:$|[\s\-_])', folder_name): phones.append(m.group(1)+m.group(2))
    for m in re.finditer(r'(?:^|[\s\-_])(\+|00)?(\d{10,15})(?:$|[\s\-_])', folder_name): phones.append(m.group(2))

    norm_phones = []
    for p in phones:
        if p.startswith("03") and len(p)==11: norm_phones.append("92"+p[1:])
        elif p.startswith("3") and len(p)==10: norm_phones.append("92"+p)
        elif p.startswith("00"): norm_phones.append(p[2:])
        elif p.startswith("0"): norm_phones.append(p[1:])
        else: norm_phones.append(p)
    norm_phones = list(set(norm_phones))

    code = re.search(r'---\s*(\d{5})\s*---', folder_name)
    parts = re.split(r'\s*-{2,3}\s*|\s+--\s+', folder_name)
    parts = [p.strip().lower() for p in parts if p.strip()]

    source = "Unknown"
    keywords = ['website', 'web', 'whatsapp', 'whats app', 'wa', 'insta', 'instagram', 'facebook', 'fb']
    for p in parts:
        if any(k in p for k in keywords):
            if 'whats' in p or 'wa'==p: source = 'WhatsApp'
            elif 'web' in p: source = 'Website'
            elif 'insta' in p: source = 'Instagram'
            else: source = p.title()
            break

    return {"folder_name": folder_name, "phones": norm_phones, "order_code": code.group(1) if code else None, "source": source}

# ====================================================
# 3. WHATSAPP SENDER
# ====================================================

def normalize_phone(phone):
    if not phone: return ""
    p = re.sub(r"\D", "", str(phone))
    if p.startswith("03") and len(p) == 11: return "92" + p[1:]
    return p

def send_file_via_meta_and_db(phone, file_bytes, filename, mime_type, caption):
    clean_phone = normalize_phone(phone)
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    acc = cur.fetchone()
    if not acc:
        cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        acc = cur.fetchone()
    cur.close(); conn.close()

    if not acc: raise Exception("No DB Account")

    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"
    headers = {"Authorization": f"Bearer {acc['access_token']}"}
    files = {'file': (filename, file_bytes, mime_type), 'messaging_product': (None, 'whatsapp')}

    r = requests.post(url, headers=headers, files=files)
    if r.status_code != 200: raise Exception(f"Upload: {r.text}")

    msg_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    msg_type = "image" if "image" in mime_type else "document"
    payload = {"messaging_product": "whatsapp", "to": clean_phone, "type": msg_type}
    if msg_type == "image": payload["image"] = {"id": r.json()['id'], "caption": caption}
    else: payload["document"] = {"id": r.json()['id'], "caption": caption, "filename": filename}

    r2 = requests.post(msg_url, headers={'Authorization': f"Bearer {acc['access_token']}", 'Content-Type': 'application/json'}, json=payload)
    if r2.status_code not in [200, 201]: raise Exception(f"Send: {r2.text}")

    wa_id = r2.json().get('messages', [{}])[0].get('id')
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO messages (whatsapp_account_id, user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp) VALUES (%s,%s,'agent',%s,%s,%s,%s,'sent',NOW())",
                (acc['id'], clean_phone, msg_type, r.json()['id'], caption, wa_id))
    conn.commit(); cur.close(); conn.close()
    return True

def send_text_via_meta_and_db(phone, text):
    clean_phone = normalize_phone(phone)
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    acc = cur.fetchone() or {}
    if not acc: return False

    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    payload = {"messaging_product": "whatsapp", "to": clean_phone, "type": "text", "text": {"body": text}}
    r = requests.post(url, headers={'Authorization': f"Bearer {acc['access_token']}", 'Content-Type': 'application/json'}, json=payload)
    wa_id = r.json().get('messages', [{}])[0].get('id')

    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp) VALUES (%s,%s,'agent',%s,%s,'sent',NOW())",
                (acc['id'], clean_phone, text, wa_id))
    conn.commit(); cur.close(); conn.close()
    return True

# ====================================================
# 🟢 CRON JOB LOGIC
# ====================================================

def run_scheduled_automation():
    if os.getenv("ENABLE_CRON") != "true":
        return

    logging.warning(f"[CRON] Starting Auto Design Check at {datetime.utcnow()}")
    init_log_table()

    dbx = get_system_dropbox_client()
    if not dbx:
        print("❌ [CRON] Dropbox client not available", flush=True)
        return

    # ====================================================
    # 1️⃣ SCAN DROPBOX
    # ====================================================
    all_folders = []
    for path in TARGET_PATHS:
        try:
            res = dbx.files_list_folder(path)
            all_folders.extend(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                all_folders.extend(res.entries)
        except Exception as e:
            print("❌ [CRON] Dropbox scan error:", e, flush=True)

    folder_metas = [
        e for e in all_folders
        if isinstance(e, dropbox.files.FolderMetadata)
    ]

    # ====================================================
    # 2️⃣ PARSE & COLLECT PHONES (OLD LOGIC)
    # ====================================================
    candidates = []
    all_phones = set()

    for folder in folder_metas:
        name_lower = folder.name.lower()

        if "incomplete" in name_lower:
            continue

        if any(ign in name_lower for ign in IGNORED_FOLDERS):
            continue

        d = parse_folder_name(folder.name)
        d["full_path"] = folder.path_lower
        d["display_path"] = folder.path_display

        if d["phones"]:
            candidates.append(d)
            for p in d["phones"]:
                # 🔑 OLD LOGIC — last 10 digits
                all_phones.add(p[-10:])

    # ====================================================
    # 3️⃣ FETCH CUSTOMER REPLIES (OLD LOGIC)
    # ====================================================
    responded_set = set()
    sent_set = set()

    if all_phones:
        conn = get_conn()
        cur = conn.cursor()

        chk = list(all_phones)
        fmt = ",".join(["%s"] * len(chk))

        cur.execute(
            f"""
            SELECT DISTINCT RIGHT(user_phone, 10)
            FROM messages
            WHERE sender = 'customer'
              AND RIGHT(user_phone, 10) IN ({fmt})
            """,
            tuple(chk)
        )

        for r in cur.fetchall():
            responded_set.add(r[0])

        cur.execute("SELECT folder_name FROM design_sent_log")
        for r in cur.fetchall():
            sent_set.add(r[0])

        cur.close()
        conn.close()

    # ====================================================
    # 4️⃣ PROCESS CANDIDATES (OLD DECISION LOGIC)
    # ====================================================
    for item in candidates:
        if item["folder_name"] in sent_set:
            continue

        active_phone = item["phones"][0]
        has_replied = False

        for p in item["phones"]:
            if p[-10:] in responded_set:
                active_phone = p
                has_replied = True
                break

        is_wa = "whatsapp" in item["source"].lower()

        # 🔑 OLD BEHAVIOR: send only if WhatsApp OR replied
        if not (is_wa or has_replied):
            continue

        # ====================================================
        # 🔒 LOCK
        # ====================================================
        if not attempt_to_claim_folder(
            item["folder_name"],
            active_phone,
            method="cron"
        ):
            continue

        try:
            files = dbx.files_list_folder(item["display_path"]).entries
            pngs = [
                f for f in files
                if isinstance(f, dropbox.files.FileMetadata)
                and f.name.lower().endswith(".png")
            ]

            if not pngs:
                release_lock_if_safe(item["folder_name"], False)
                continue

            # ⏱ Time check (5 minutes)
            now = datetime.utcnow()
            should_send = False
            for f in pngs:
                if (now - f.server_modified) > timedelta(minutes=5):
                    should_send = True
                    break

            if not should_send:
                release_lock_if_safe(item["folder_name"], False)
                continue

            #print(f"🚀 [CRON] Sending '{item['folder_name']}'", flush=True)
            logging.warning(f"[CRON] Sending folder: {item['folder_name']}")

            sent_any = False
            for i, f in enumerate(pngs):
                if i > 0:
                    time.sleep(15)

                _, res = dbx.files_download(f.path_lower)
                caption = os.path.splitext(f.name)[0]

                send_file_via_meta_and_db(
                    active_phone,
                    res.content,
                    f.name,
                    "image/png",
                    caption
                )

                sent_any = True

            if sent_any:
                send_text_via_meta_and_db(
                    active_phone,
                    "Please confirm text and design.\n"
                    "No changes will be made after confirmation.\n"
                    "If there is any correction - please reply to image for faster response"
                )

                update_sent_status(
                    item["folder_name"],
                    f"{len(pngs)} files",
                    method="cron"
                )

                move_folder_after_sending(
                    dbx,
                    item["display_path"],
                    item["folder_name"]
                )

            else:
                release_lock_if_safe(item["folder_name"], False)

        except Exception as e:
            print(f"❌ [CRON ERROR] {item['folder_name']}: {e}", flush=True)
            release_lock_if_safe(item["folder_name"], False)

    print("✅ [CRON] Finished Cycle", flush=True)

# ====================================================
# 5. ROUTES
# ====================================================

@design_sender_bp.route("/automation/preview")
def preview_automation():
    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)

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
    parsed_list = []
    all_phones = set()

    for name in folder_names:
        name_lower = name.lower()
        if "incomplete" in name_lower: continue
        if any(ign in name_lower for ign in IGNORED_FOLDERS): continue
        d = parse_folder_name(name)
        if d['phones']:
            parsed_list.append(d)
            for p in d['phones']: all_phones.add(short_pk(p))


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
    from app import PENDING_DESIGN_CONFIRMATION # Import here

    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(); folders = data.get('folders', [])
    if not folders: return jsonify({"sent_count": 0, "errors": ["No folders"]})

    init_log_table()
    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)
    sent_count = 0; errors = []

    for item in folders:
        folder = item['folder_name']; phone = item.get('phone') or item.get('active_phone')

        # LOCKING
        if not attempt_to_claim_folder(folder, phone, method='manual_batch'):
            continue

        full_path = item.get('full_path')
        if not full_path:
            for base in TARGET_PATHS:
                try: t=f"{base}/{folder}"; dbx.files_get_metadata(t); full_path=t; break
                except: continue

        if not full_path:
            release_lock_if_safe(folder, False)
            errors.append(f"{folder}: Not found")
            continue

        try:
            entries = dbx.files_list_folder(full_path).entries
            pngs = [e for e in entries if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith('.png')]

            sent_any = False
            for i, f in enumerate(pngs):
                if i > 0: time.sleep(15)
                _, res = dbx.files_download(f.path_lower)
                caption = os.path.splitext(f.name)[0]
                send_file_via_meta_and_db(phone, res.content, f.name, "image/png", caption)
                sent_any = True

            if sent_any:
                send_text_via_meta_and_db(phone, "Please confirm text and design.\nNo changes will be made after confirmation.\nIf there is any correction - please reply to image for faster response")
                update_sent_status(folder, f"{len(pngs)} files", method='manual_batch')
                move_folder_after_sending(dbx, full_path, folder)
                sent_count += 1

                # ✅ CORRECTED: Using 'phone' variable, not 'active_phone'
                PENDING_DESIGN_CONFIRMATION[normalize_phone(phone)] = {
                    "ts": time.time(),
                    "source": "auto_design_prompt"
                }

            else:
                release_lock_if_safe(folder, False)

        except Exception as e:
            release_lock_if_safe(folder, False)
            errors.append(f"{folder}: {e}")

    return jsonify({"status": "success", "sent_count": sent_count, "errors": errors})


@design_sender_bp.route("/manual_send_design", methods=['POST'])
def manual_send_design():
    """MANUAL SINGLE BUTTON"""
    from app import PENDING_DESIGN_CONFIRMATION # Import here

    if "user_id" not in session: return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    folder = data.get('folder_name')
    phone = data.get('phone')
    path = data.get('full_path')

    if not folder or not phone: return jsonify({"error": "Missing data"}), 400

    init_log_table()

    # LOCKING
    if not attempt_to_claim_folder(folder, phone, method='manual_single'):
        return jsonify({"error": "⚠️ Design already sent/processing!"}), 409

    user_id = session["user_id"]
    dbx = get_dropbox_client(user_id)
    sent_count = 0; errors = []

    try:
        entries = []; res = dbx.files_list_folder(path)
        entries.extend(res.entries)
        while res.has_more: res = dbx.files_list_folder_continue(res.cursor); entries.extend(res.entries)
        pngs = [e for e in entries if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith('.png')]

        if not pngs:
            release_lock_if_safe(folder, False)
            return jsonify({"error": "No .png files found"}), 404

        for i, f in enumerate(pngs):
            if i > 0: time.sleep(15)
            try:
                _, res = dbx.files_download(f.path_lower)
                caption = os.path.splitext(f.name)[0]
                send_file_via_meta_and_db(phone, res.content, f.name, "image/png", caption)
                sent_count += 1
            except Exception as e: errors.append(f"{f.name}: {str(e)}")

        if sent_count > 0:
            # ✅ CORRECTED: Fixed indentation and using 'phone'
            try:
                send_text_via_meta_and_db(phone, "Please confirm text and design.\nNo changes will be made after confirmation.\nIf there is any correction - please reply to image for faster response")
                PENDING_DESIGN_CONFIRMATION[normalize_phone(phone)] = {
                    "ts": time.time(),
                    "source": "auto_design_prompt"
                }
            except Exception as e:
                print("❌ Failed to set confirmation or send text:", e)

            update_sent_status(folder, f"{sent_count} PNGs", method='manual_single')
            move_folder_after_sending(dbx, path, folder)

        return jsonify({"status": "success", "sent_count": sent_count, "total_found": len(pngs), "errors": errors})
    except Exception as e:
        release_lock_if_safe(folder, sent_count > 0)
        return jsonify({"error": str(e)}), 500


def short_pk(phone):
    p = re.sub(r"\D", "", phone)
    if p.startswith("92"):
        return p[2:]   # 10 digits
    return p[-10:]
