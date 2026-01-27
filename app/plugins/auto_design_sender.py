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
def normalize_phone_meta(p):
    if not p:
        return None

    # 1️⃣ Keep digits only
    p = "".join(filter(str.isdigit, str(p)))

    # 2️⃣ Remove international prefix 00 (0092, 00971 etc)
    if p.startswith("00"):
        p = p[2:]

    # -------------------------
    # 🇵🇰 Pakistan Numbers
    # -------------------------

    # ✅ AUTO-FIX: Missing leading zero (10 digits) → assume 03XXXXXXXXX
    # Example: 3008204180 → 03008204180
    if len(p) == 10 and p.startswith("3"):
        p = "0" + p

    # Local mobile format: 03XXXXXXXXX → 92XXXXXXXXXX
    if p.startswith("03") and len(p) == 11:
        return "92" + p[1:]

    # Already proper Pakistan international
    if p.startswith("92") and len(p) == 12:
        return p

    # -------------------------
    # 🇦🇪 UAE Numbers
    # -------------------------

    # UAE local format: 05XXXXXXXX → 971XXXXXXXX
    if p.startswith("05") and len(p) == 10:
        return "971" + p[1:]

    # Already international UAE
    if p.startswith("971") and len(p) >= 11:
        return p

    # -------------------------
    # 🌍 Generic International
    # -------------------------

    # If already long enough — trust it
    if len(p) >= 11:
        return p

    # ❌ Reject short garbage numbers
    return None

def log_skip(reason, folder, extra=""):
    logging.warning(f"[SKIP][{reason}] folder={folder} {extra}")


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

    nums = re.findall(r'\+?\d{10,15}', folder_name)

    phones = []

    for n in nums:
        n = normalize_phone_meta(n)
        if n:
            phones.append(n)

    return {
        "folder_name": folder_name,
        "phones": list(set(phones))
    }


# ====================================================
# WHATSAPP SENDERS
# ====================================================



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
    # Scan Dropbox (FIRST LEVEL)
    # ---------------------------

    all_folders = []

    for path in TARGET_PATHS:

        try:
            res = dbx.files_list_folder(path)

            all_folders.extend(res.entries)

            batch = 1
            logging.warning(f"[CRON] Batch {batch}: {len(res.entries)} items")

            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                batch += 1
                all_folders.extend(res.entries)
                logging.warning(f"[CRON] Batch {batch}: {len(res.entries)} items")

        except Exception as e:
            logging.error(f"[CRON] Dropbox error: {e}")

    folder_list = [
        f for f in all_folders
        if isinstance(f, dropbox.files.FolderMetadata)
    ]

    candidates = []
    all_phones = set()

    # ---------------------------
    # Parse folders
    # ---------------------------

    for folder in folder_list:

        logging.warning(f"[SCAN] folder_name={folder.name}")

        name = folder.name.lower()

        if any(x in name for x in IGNORED_FOLDERS):
            log_skip("IGNORED", folder.name)
            continue

        parsed = parse_folder_name(folder.name)

        if not parsed["phones"]:
            log_skip("NO_PHONE", folder.name)
            continue

        parsed["display_path"] = folder.path_lower

        candidates.append(parsed)

        for p in parsed["phones"]:
            clean = re.sub(r'\D', '', p)
            if len(clean) >= 10:
                all_phones.add(clean[-10:])

    logging.warning(f"[DEBUG] all_phones count={len(all_phones)}")

    # ---------------------------
    # Fetch LAST CUSTOMER reply
    # ---------------------------

    responded_recent = {}

    if all_phones:

        conn = get_conn()
        cur = conn.cursor()

        phone_list = list(all_phones)

        cur.execute("""
            SELECT CAST(RIGHT(user_phone,10) AS TEXT), MAX(timestamp)
            FROM messages
            WHERE sender = 'customer'
              AND (is_legacy = FALSE OR is_legacy IS NULL)
              AND CAST(RIGHT(user_phone,10) AS TEXT) = ANY(%s::text[])
            GROUP BY CAST(RIGHT(user_phone,10) AS TEXT)
        """, (phone_list,))

        rows = cur.fetchall()

        logging.warning(f"[SQL RAW RESULT] {rows}")

        for row in rows:
            phone10 = row['right']
            ts = row['max']
            responded_recent[phone10] = ts


        logging.warning(f"[DEBUG] responded_recent keys = {list(responded_recent.keys())}")

        cur.close()
        conn.close()

    # ---------------------------
    # PROCESS FOLDERS
    # ---------------------------

    for idx, item in enumerate(candidates, start=1):

        logging.warning(
            f"[FOLDER {idx}/{len(candidates)}] "
            f"name={item['folder_name']} phones={item['phones']}"
        )

        active_phone = None
        has_recent_reply = False

        # ---------------------------
        # Match phone
        # ---------------------------

        for p in item["phones"]:

            raw_phone = p
            clean_p = re.sub(r'\D', '', p)

            if len(clean_p) < 10:
                logging.warning(f"[BAD PHONE] raw={raw_phone}")
                continue

            short = clean_p[-10:]

            in_db = short in responded_recent

            logging.warning(
                f"[MATCH CHECK] "
                f"folder={item['folder_name']} | "
                f"raw={raw_phone} | "
                f"short={short} | "
                f"in_db={in_db}"
            )

            if in_db:

                last_time = responded_recent[short]

                now_utc = datetime.now(timezone.utc)

                last_time_utc = (
                    last_time if last_time.tzinfo
                    else last_time.replace(tzinfo=timezone.utc)
                )

                diff_hours = (now_utc - last_time_utc).total_seconds() / 3600

                logging.warning(
                    f"[TIME CHECK] "
                    f"phone={short} diff_hours={round(diff_hours, 2)}"
                )

                if diff_hours <= 24:
                    active_phone = raw_phone
                    has_recent_reply = True
                    break

        if not has_recent_reply:
            log_skip("NO_REPLY", item["folder_name"])
            continue

        # ---------------------------
        # LOCK
        # ---------------------------

        if not attempt_to_claim_folder(item["folder_name"], active_phone):
            log_skip("LOCKED", item["folder_name"])
            continue

        sent_any = False

        try:

            files = dbx.files_list_folder(item["display_path"]).entries

            pngs = [
                f for f in files
                if isinstance(f, dropbox.files.FileMetadata)
                and f.name.lower().endswith(".png")
            ]

            if not pngs:
                log_skip("NO_PNG", item["folder_name"])
                release_lock(item["folder_name"])
                continue

            logging.warning(f"[SEND START] {item['folder_name']}")

            # ---------------------------
            # SEND FILES
            # ---------------------------

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

                sent_any = True

            # ---------------------------
            # SEND CONFIRMATION
            # ---------------------------

            if sent_any:

                confirm_msg = (
                    "Please confirm text and design.\n"
                    "No changes will be made after confirmation.\n"
                    "If there is any correction - please reply to image for faster response."
                )

                from app.app import send_text_via_meta_and_db
                send_text_via_meta_and_db(active_phone, confirm_msg)
                from app.app import add_contact_tag
                add_contact_tag(active_phone, 1)

                update_sent_status(
                    item["folder_name"],
                    f"{len(pngs)} files",
                    "cron"
                )

                moved = move_folder_after_sending(
                    dbx,
                    item["display_path"],
                    item["folder_name"]
                )

                if not moved:
                    logging.error(f"[CRON] MOVE FAILED: {item['folder_name']}")

            release_lock(item["folder_name"])

        except Exception as e:

            logging.error(f"[CRON ERROR] {item['folder_name']} : {e}")

            if not sent_any:
                release_lock(item["folder_name"])
