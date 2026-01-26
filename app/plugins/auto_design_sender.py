import os
import re
import time
import json
import requests
import dropbox
import mimetypes
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, session, request
from app.db import get_conn
from psycopg2.extras import RealDictCursor
import psycopg2
import logging
from app.constants import PENDING_DESIGN_CONFIRMATION
design_sender_bp = Blueprint("design_sender", __name__)

# --- CONFIGURATION ---
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")

#TARGET_WABA_ID = "1628402398537645"
TARGET_WABA_ID = "881106361269982"

TARGET_PATHS = [
    "/1 daniyal/Auto"
]

IGNORED_FOLDERS = [
    "instagram", "no reply", "confirm", "file issues",
    "cancelled orders", "correction done", "faraz corrections", "send to customer"
]

MOVE_DESTINATION_BASE = "/1 daniyal/Auto/send to customer"
def normalize_phone_meta(phone):
    phone = str(phone).strip()

    phone = phone.replace("+", "").replace(" ", "").replace("-", "")

    # Pakistan handling
    if phone.startswith("0"):
        phone = "92" + phone[1:]

    if phone.startswith("92") and len(phone) == 12:
        return phone

    # Fallback last 10 digits
    if len(phone) >= 10:
        return "92" + phone[-10:]

    return phone

def normalize_10(phone):
    p = "".join(filter(str.isdigit, phone))

    # Remove leading 92 if exists
    if p.startswith("92") and len(p) > 10:
        p = p[2:]

    return p[-10:]

# ====================================================
# DB + DROPBOX HELPERS
# ====================================================
def get_active_whatsapp_account_id():

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT id
        FROM whatsapp_accounts
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return None

    return row["id"]
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
    cur.close()
    conn.close()


def get_system_dropbox_client():

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT user_id, access_token, refresh_token
        FROM dropbox_accounts
        LIMIT 1
    """)

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return None

    uid = row["user_id"]
    at = row["access_token"]
    rt = row["refresh_token"]

    try:
        dbx = dropbox.Dropbox(at)
        dbx.users_get_current_account()
        return dbx

    except:

        try:
            token_url = "https://api.dropboxapi.com/oauth2/token"

            data = {
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": APP_KEY,
                "client_secret": APP_SECRET
            }

            r = requests.post(token_url, data=data).json()
            new_at = r.get("access_token")

            if not new_at:
                logging.error("[DROPBOX] Token refresh failed")
                return None

            conn = get_conn()
            cur = conn.cursor()

            cur.execute("""
                UPDATE dropbox_accounts
                SET access_token=%s
                WHERE user_id=%s
            """, (new_at, uid))

            conn.commit()
            cur.close()
            conn.close()

            return dropbox.Dropbox(new_at)

        except Exception as e:
            logging.error(f"[DROPBOX] Auth Error: {e}")
            return None


def attempt_to_claim_folder(folder_name, phone, method='cron'):

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("SELECT 1 FROM design_sent_log WHERE folder_name=%s", (folder_name,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return False

        cur.execute("""
            INSERT INTO design_sent_log (folder_name, phone_number, status, sent_method)
            VALUES (%s,%s,'processing',%s)
        """, (folder_name, phone, method))

        conn.commit()
        cur.close()
        conn.close()
        return True

    except psycopg2.IntegrityError:
        conn.rollback()
        cur.close()
        conn.close()
        return False


def update_sent_status(folder_name, file_name, method):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE design_sent_log
        SET status='sent', file_name=%s, sent_at=NOW(), sent_method=%s
        WHERE folder_name=%s
    """, (file_name, method, folder_name))

    conn.commit()
    cur.close()
    conn.close()


def release_lock(folder_name):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM design_sent_log WHERE folder_name=%s", (folder_name,))
    conn.commit()
    cur.close()
    conn.close()


def move_folder_after_sending(dbx, current_path, folder_name):

    target = f"{MOVE_DESTINATION_BASE}/{folder_name}"

    try:
        dbx.files_move_v2(from_path=current_path, to_path=target)
        logging.warning(f"[MOVE] {folder_name} -> send to customer")
        return True

    except Exception as e:
        logging.error(f"[MOVE FAILED] {e}")
        return False


# ====================================================
# PARSE FOLDER
# ====================================================

def parse_folder_name(folder_name):

    phones = set()

    # 🔹 PRIMARY MATCH (structured)
    for m in re.finditer(r'(?:^|[\s\-_])((?:0092|92|0)?3\d{2})\s*(\d{7})', folder_name):
        phones.add(m.group(1) + m.group(2))

    # 🔹 FALLBACK MATCH (any long number anywhere)
    for m in re.findall(r'(?:0092|92|0)?3\d{9}', folder_name):
        phones.add(m)

    norm = []

    for p in phones:
        p = re.sub(r"\D", "", p)

        if p.startswith("0092"):
            p = p[4:]
        if p.startswith("03"):
            p = "92" + p[1:]
        elif p.startswith("3"):
            p = "92" + p

        if len(p) == 12:
            norm.append(p)

    return {
        "folder_name": folder_name,
        "phones": list(set(norm))
    }


# ====================================================
# WHATSAPP SENDERS
# ====================================================

def normalize_phone(phone):
    if not phone:
        return ""
    p = re.sub(r"\D", "", str(phone))

    # Pakistan mobile handling
    if p.startswith("03") and len(p) == 11:
        return "92" + p[1:]

    # Already international
    if p.startswith("92") and len(p) == 12:
        return p

    return p


def send_file_via_meta_and_db(phone, file_bytes, filename, mime_type, caption):


    phone = normalize_phone_meta(phone)

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT id, phone_number_id, access_token
        FROM whatsapp_accounts
        WHERE waba_id=%s LIMIT 1
    """, (TARGET_WABA_ID,))

    acc = cur.fetchone()

    cur.close()
    conn.close()

    if not acc:
        raise Exception("No WhatsApp account")

    upload_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"

    headers = {"Authorization": f"Bearer {acc['access_token']}"}

    files = {
        "file": (filename, file_bytes, mime_type),
        "messaging_product": (None, "whatsapp")
    }

    r = requests.post(upload_url, headers=headers, files=files)

    if r.status_code != 200:
        raise Exception("Upload failed")

    media_id = r.json()["id"]

    msg_type = "image" if "image" in mime_type else "document"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": msg_type,
        msg_type: {
            "id": media_id,
            "caption": caption
        }
    }

    send_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"

    r2 = requests.post(send_url,
                       headers={"Authorization": f"Bearer {acc['access_token']}",
                                "Content-Type": "application/json"},
                       json=payload)

    wa_id = r2.json()["messages"][0]["id"]

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO messages
        (whatsapp_account_id,user_phone,sender,media_type,media_id,message,whatsapp_id,status,timestamp)
        VALUES (%s,%s,'agent',%s,%s,%s,%s,'sent',NOW())
    """, (acc["id"], phone, msg_type, media_id, caption, wa_id))

    conn.commit()
    cur.close()
    conn.close()


# ====================================================
# CRON JOB (STRICT 24H RULE)
# ====================================================

def run_scheduled_automation():

    if os.getenv("ENABLE_CRON") != "true":
        return

    logging.warning("[CRON] Auto Design Sender Running")


    init_log_table()

    dbx = get_system_dropbox_client()
    if not dbx:
        logging.error("[CRON] Dropbox auth failed")
        return

    # ---------------------------
    # Scan Dropbox
    # ---------------------------

    all_folders = []

    for path in TARGET_PATHS:

        res = dbx.files_list_folder(path)

        all_folders.extend(res.entries)

        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            all_folders.extend(res.entries)

    folder_list = [
        f for f in all_folders
        if isinstance(f, dropbox.files.FolderMetadata)
    ]

    candidates = []
    all_phones = set()

    for folder in folder_list:

        name = folder.name.lower()

        if any(x in name for x in IGNORED_FOLDERS):
            continue

        parsed = parse_folder_name(folder.name)

        if not parsed["phones"]:
            continue

        parsed["display_path"] = folder.path_display

        candidates.append(parsed)

        for p in parsed["phones"]:
            clean = re.sub(r'\D', '', p)
            all_phones.add(clean[-10:])

    # ---------------------------
    # Fetch LAST CUSTOMER reply time
    # ---------------------------

    responded_recent = {}

    if all_phones:

        conn = get_conn()
        cur = conn.cursor()

        fmt = ",".join(["%s"] * len(all_phones))

        active_account_id = get_active_whatsapp_account_id()

        if not active_account_id:
            logging.error("[CRON] No active WhatsApp account found")
            return

        cur.execute(f"""
            SELECT
                RIGHT(REGEXP_REPLACE(user_phone, '[^0-9]', '', 'g'), 10) as phone10,
                MAX(timestamp)
            FROM messages
            WHERE sender='customer'
              AND whatsapp_account_id = %s
              AND is_legacy = FALSE
              AND RIGHT(REGEXP_REPLACE(user_phone, '[^0-9]', '', 'g'), 10) IN ({fmt})
            GROUP BY phone10
        """, (active_account_id, *all_phones))

        rows = cur.fetchall()

        for r in rows:
            phone10 = r["phone10"]
            ts = r["max"]

            responded_recent[phone10] = ts

        cur.close()
        conn.close()

    # ---------------------------
    # PROCESS
    # ---------------------------

    for item in candidates:

        active_phone = None
        has_recent_reply = False

        for p in item["phones"]:

            short = p[-10:]

            if short in responded_recent:

                last_time = responded_recent[short]

                # Force DB timestamp to UTC (Postgres returns naive datetime)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)

                if datetime.now(timezone.utc) - last_time <= timedelta(hours=24):
                    active_phone = p
                    has_recent_reply = True
                    break

        # ❌ STRICT RULE: NO REPLY = NO SEND
        if not has_recent_reply:
            logging.warning(f"[CRON DEBUG] Folder phones: {item['phones']}")
            logging.warning(f"[CRON DEBUG] DB responded phones: {responded_recent}")

            logging.warning(f"[CRON] SKIPPED {item['folder_name']} (24h expired)")
            continue

        if not attempt_to_claim_folder(item["folder_name"], active_phone):
            continue

        try:

            files = dbx.files_list_folder(item["display_path"]).entries

            pngs = [
                f for f in files
                if isinstance(f, dropbox.files.FileMetadata)
                and f.name.lower().endswith(".png")
            ]

            if not pngs:
                release_lock(item["folder_name"])
                continue

            logging.warning(f"[CRON] Sending {item['folder_name']}")

            for i, f in enumerate(pngs):

                if i > 0:
                    time.sleep(10)

                _, res = dbx.files_download(f.path_lower)

                caption = os.path.splitext(f.name)[0]

                send_file_via_meta_and_db(
                    active_phone,
                    res.content,
                    f.name,
                    "image/png",
                    caption
                )

            update_sent_status(item["folder_name"], f"{len(pngs)} files", "cron")
            send_text_via_meta_and_db(
                active_phone,
                "Please confirm text and design.\n"
                "No changes will be made after confirmation.\n"
                "If there is any correction - please reply to image for faster response"
            )
            PENDING_DESIGN_CONFIRMATION[normalize_phone(active_phone)] = {
                "ts": time.time(),
                "source": "auto_design_prompt"
            }
            move_folder_after_sending(dbx,
                                      item["display_path"],
                                      item["folder_name"])

        except Exception as e:

            logging.error(f"[CRON ERROR] {item['folder_name']} : {e}")

            release_lock(item["folder_name"])

    logging.warning("[CRON] Finished Cycle")
