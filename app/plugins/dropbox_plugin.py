import os
import requests
import dropbox
import urllib.parse
import re
from flask import Blueprint, request, redirect, session, jsonify, render_template
from db import get_conn
from datetime import datetime, timedelta
dropbox_bp = Blueprint("dropbox", __name__)

# --- CONFIGURATION ---
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
REDIRECT_URI = os.getenv("DROPBOX_REDIRECT_URI")

assert APP_KEY, "DROPBOX_APP_KEY missing"
assert APP_SECRET, "DROPBOX_APP_SECRET missing"
assert REDIRECT_URI, "DROPBOX_REDIRECT_URI missing"

# ==========================================
# 1. AUTHENTICATION
# ==========================================

@dropbox_bp.route("/dropbox/connect")
def dropbox_connect():
    if "user_id" not in session: return redirect("/login")
    params = {
        "client_id": APP_KEY, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "token_access_type": "offline"
    }
    return redirect("https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(params))

@dropbox_bp.route("/dropbox/callback")
def dropbox_callback():
    if "user_id" not in session: return redirect("/login")
    code = request.args.get("code")
    if not code: return "Dropbox authorization failed", 400

    token_url = "https://api.dropboxapi.com/oauth2/token"
    data = {
        "code": code, "grant_type": "authorization_code",
        "client_id": APP_KEY, "client_secret": APP_SECRET,
        "redirect_uri": REDIRECT_URI
    }
    r = requests.post(token_url, data=data)
    tokens = r.json()

    if r.status_code != 200: return jsonify(tokens), 400

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    user_id = session["user_id"]

    conn = get_conn()
    cur = conn.cursor()

    if refresh_token:
        cur.execute("""
            INSERT INTO dropbox_accounts (user_id, access_token, refresh_token)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_updated_at = NOW()
        """, (user_id, access_token, refresh_token))
    else:
        cur.execute("""
            INSERT INTO dropbox_accounts (user_id, access_token)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                token_updated_at = NOW()
        """, (user_id, access_token))

    conn.commit()
    cur.close()
    conn.close()
    return redirect("/inbox")

# ==========================================
# 2. CLIENT HELPER
# ==========================================

def get_user_dropbox_client(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT access_token, refresh_token FROM dropbox_accounts WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row: raise Exception("Dropbox not connected")

    if isinstance(row, dict):
        access_token = row['access_token']
        refresh_token = row['refresh_token']
    else:
        access_token, refresh_token = row

    try:
        dbx = dropbox.Dropbox(access_token)
        dbx.users_get_current_account()
        return dbx
    except dropbox.exceptions.AuthError:
        new_token = refresh_access_token(refresh_token)
        save_tokens(user_id, new_token, refresh_token)
        return dropbox.Dropbox(new_token)

def refresh_access_token(refresh_token):
    url = "https://api.dropboxapi.com/oauth2/token"
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": APP_KEY, "client_secret": APP_SECRET}
    r = requests.post(url, data=data)
    if r.status_code != 200: raise Exception(r.json())
    return r.json()["access_token"]

def save_tokens(user_id, access_token, refresh_token):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE dropbox_accounts SET access_token = %s, refresh_token = %s, token_updated_at = NOW() WHERE user_id = %s", (access_token, refresh_token, user_id))
    conn.commit()
    cur.close()
    conn.close()

# ==========================================
# ==========================================
# 3. MAIN LOGIC (PARSING & DB CHECK)
# ==========================================

def parse_folder_data(folder_name):
    """
    Dynamically finds Source (WhatsApp/Website) and Name.
    """
    # 1. FIND ALL NUMBERS
    phone_iterator = re.finditer(r'(?:\s|^|\-)(\+|00)?(\d{10,15})(?:\s|$|\-)', folder_name)
    found_phones = []

    for match in phone_iterator:
        raw_digits = match.group(2)
        normalized = raw_digits
        if raw_digits.startswith("03") and len(raw_digits) == 11:
            normalized = "92" + raw_digits[1:]
        elif raw_digits.startswith("3") and len(raw_digits) == 10:
            normalized = "92" + raw_digits
        found_phones.append(normalized)

    found_phones = list(set(found_phones))

    # 2. Extract Order Code
    order_code = None
    code_match = re.search(r'---\s*(\d{5})\s*---', folder_name)
    if code_match:
        order_code = code_match.group(1)

    # 3. Extract Source & Name (Robust Split)
    parts = re.split(r'\s*-{2,3}\s*|\s+--\s+', folder_name)
    parts = [p.strip() for p in parts if p.strip()]

    source = "Unknown"
    customer_name = "Unknown"

    keywords = ['website', 'whatsapp', 'instagram', 'facebook', 'complain']

    source_index = -1

    # Search for keywords
    for i, part in enumerate(parts):
        # Skip if looks like phone or code
        if i == 0 and re.search(r'\d{10}', part): continue
        if re.match(r'^\d{5}$', part): continue

        val = part.lower()
        if any(k in val for k in keywords):
            source_index = i
            # Normalize Source Name
            if 'whats' in val or 'wa' == val: source = 'WhatsApp'
            elif 'web' in val: source = 'Website'
            elif 'insta' in val: source = 'Instagram'
            else: source = part.title()
            break

    if source_index != -1 and len(parts) > source_index + 1:
        customer_name = parts[source_index + 1]
    elif len(parts) >= 4:
         if not re.search(r'\d', parts[2]):
             source = parts[2]
             customer_name = parts[3]

    return {
        "folder_name": folder_name,
        "phones": found_phones,
        "order_code": order_code,
        "source": source,
        "customer_name": customer_name
    }

def get_all_dropbox_folders(dbx, path):
    all_entries = []
    try:
        result = dbx.files_list_folder(path)
        all_entries.extend(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            all_entries.extend(result.entries)
    except dropbox.exceptions.ApiError: return []
    return [e.name for e in all_entries if isinstance(e, dropbox.files.FolderMetadata)]

@dropbox_bp.route("/auto_no_response")
def auto_no_response():
    if "user_id" not in session: return redirect("/login")

    user_id = session["user_id"]
    dbx = get_user_dropbox_client(user_id)
    path = "/1 daniyal/Auto"

    # 1. Get Folders
    folder_names = get_all_dropbox_folders(dbx, path)

    parsed_folders = []
    unparsed_folders = []
    all_candidate_phones = set()

    # 2. Parse Data
    for name in folder_names:
        data = parse_folder_data(name)
        if data["phones"]:
            parsed_folders.append(data)
            for p in data["phones"]:
                all_candidate_phones.add(p[-10:])
        else:
            unparsed_folders.append(name)

    # 3. Check Database
    responded_short_numbers = set()

    if all_candidate_phones:
        conn = get_conn(); cur = conn.cursor()
        check_list = list(all_candidate_phones)
        format_strings = ','.join(['%s'] * len(check_list))

        # Check messages table
        query = f"""
            SELECT DISTINCT RIGHT(user_phone, 10) as short_phone
            FROM messages
            WHERE sender = 'customer'
            AND RIGHT(user_phone, 10) IN ({format_strings})
        """
        cur.execute(query, tuple(check_list))
        rows = cur.fetchall()

        if rows:
            if isinstance(rows[0], dict): responded_short_numbers = {row['short_phone'] for row in rows}
            else: responded_short_numbers = {row[0] for row in rows}
        cur.close(); conn.close()

    # 4. CATEGORIZE
    users_responded = []
    users_no_response = []

    for item in parsed_folders:
        active_phone = item["phones"][0]
        phone_match_in_db = False

        # Check DB match
        for p in item["phones"]:
            if p[-10:] in responded_short_numbers:
                active_phone = p
                phone_match_in_db = True
                break

        display_data = {
            "phone": active_phone,
            "order_code": item["order_code"],
            "source": item["source"],
            "customer_name": item["customer_name"]
        }

        # --- NEW LOGIC ---
        source_lower = item["source"].lower()
        is_website = "website" in source_lower

        if not is_website:
            # Rule 1: NOT Website (WhatsApp, Insta, etc.) -> Automatically "Replied"
            users_responded.append(display_data)

        elif is_website and phone_match_in_db:
            # Rule 2: Website AND user messaged us -> "Replied"
            users_responded.append(display_data)

        else:
            # Rule 3: Website AND NO message -> "No Response" (Waiting)
            users_no_response.append(display_data)

    # 5. Render
    return render_template(
        "dropbox_orders.html",
        total=len(folder_names),
        responded=users_responded,
        no_response=users_no_response,
        unparsed=unparsed_folders
    )


@dropbox_bp.route("/auto_correction_status")
def auto_correction_status():
    if "user_id" not in session: return redirect("/login")

    user_id = session["user_id"]
    dbx = get_user_dropbox_client(user_id)

    # 1. Define Paths
    target_paths = [
        "/1 daniyal/Auto/send to customer/Correction done",
        "/1 daniyal/Auto/send to customer"
    ]

    # Folders to completely ignore (Exact name, case-insensitive)
    ignored_folders = {
        "instagram", "no reply", "confirm", "file issues",
        "cancelled orders", "correction done", "faraz corrections"
    }

    # 2. Get Folders
    folder_names = []
    for path in target_paths:
        current_folders = get_all_dropbox_folders(dbx, path)
        folder_names.extend(current_folders)

    folder_names = list(set(folder_names))

    parsed_folders = []
    unparsed_list = []
    all_candidate_phones = set()

    for name in folder_names:
        # SKIP IGNORED FOLDERS
        if name.lower() in ignored_folders:
            continue

        data = parse_folder_data(name)
        if data["phones"]:
            parsed_folders.append(data)
            for p in data["phones"]:
                all_candidate_phones.add(p[-10:])
        else:
            # Only add to unparsed if not ignored
            unparsed_list.append(name)

    # 3. Check DB
    phone_timestamps = {}

    if all_candidate_phones:
        conn = get_conn()
        cur = conn.cursor()
        check_list = list(all_candidate_phones)
        format_strings = ','.join(['%s'] * len(check_list))

        query = f"""
            SELECT RIGHT(user_phone, 10) as short_phone, MAX(timestamp) as last_inbound
            FROM messages
            WHERE sender = 'customer'
            AND RIGHT(user_phone, 10) IN ({format_strings})
            GROUP BY RIGHT(user_phone, 10)
        """
        cur.execute(query, tuple(check_list))
        rows = cur.fetchall()
        for row in rows:
            if isinstance(row, dict): phone_timestamps[row['short_phone']] = row['last_inbound']
            else: phone_timestamps[row[0]] = row[1]
        cur.close(); conn.close()

    # 4. Calculate Time & Sort
    active_list = []
    expired_list = []
    no_chat_list = []

    now_utc = datetime.utcnow()

    for item in parsed_folders:
        matched_time = None
        active_phone = item["phones"][0]

        for p in item["phones"]:
            short_p = p[-10:]
            if short_p in phone_timestamps:
                matched_time = phone_timestamps[short_p]
                active_phone = p
                break

        display_data = {
            "phone": active_phone,
            "order_code": item["order_code"],
            "customer_name": item["customer_name"],
            "folder_name": item["folder_name"]
        }

        if matched_time:
            pkt_time = matched_time + timedelta(hours=5)
            display_data["last_msg_time"] = pkt_time.strftime("%d %b %I:%M %p")

            window_close_time = matched_time + timedelta(hours=24)
            time_left = window_close_time - now_utc
            total_seconds = time_left.total_seconds()

            if total_seconds > 0:
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                display_data["time_str"] = f"{hours}h {minutes}m"
                display_data["seconds_left"] = total_seconds
                display_data["hours_left"] = hours
                active_list.append(display_data)
            else:
                expired_seconds = abs(total_seconds)
                hours = int(expired_seconds // 3600)
                display_data["time_str"] = f"{hours}h"
                display_data["seconds_expired"] = expired_seconds
                expired_list.append(display_data)
        else:
            no_chat_list.append(display_data)

    active_list.sort(key=lambda x: x["seconds_left"])
    expired_list.sort(key=lambda x: x["seconds_expired"])

    urgent_count = sum(1 for x in active_list if x["hours_left"] < 4)

    return render_template(
        "correction_status.html",
        active_list=active_list,
        expired_list=expired_list,
        no_chat_list=no_chat_list,
        unparsed_list=unparsed_list,
        urgent_count=urgent_count,
        active_count=len(active_list),
        expired_count=len(expired_list),
        no_chat_count=len(no_chat_list),
        unparsed_count=len(unparsed_list)
    )
