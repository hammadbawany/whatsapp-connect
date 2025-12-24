import os
import requests
import dropbox
import urllib.parse
import re
from flask import Blueprint, request, redirect, session, jsonify, render_template
from db import get_conn

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
# 3. MAIN LOGIC (PARSING & DB CHECK)
# ==========================================

def parse_folder_data(folder_name):
    """
    Extracts Data from folder name.
    Identifies ALL potential phone numbers (International & Local).
    """
    # 1. FIND ALL NUMBERS (Not just the first one)
    # This regex captures the Prefix (+ or 00) and the Digits (10-15 long) separately
    phone_iterator = re.finditer(r'(?:\s|^|\-)(\+|00)?(\d{10,15})(?:\s|$|\-)', folder_name)

    found_phones = []

    for match in phone_iterator:
        prefix = match.group(1) # e.g. "00" or "+"
        raw_digits = match.group(2) # e.g. "14053149782"

        normalized = raw_digits

        # Logic to handle Pakistan 03... -> 923...
        # Only apply if it looks exactly like a local mobile number
        if raw_digits.startswith("03") and len(raw_digits) == 11:
            normalized = "92" + raw_digits[1:]

        # Logic for "3..." -> "923..." (Missing leading zero)
        elif raw_digits.startswith("3") and len(raw_digits) == 10:
            normalized = "92" + raw_digits

        # For International (e.g. 0014... or +14...), we simply keep the digits
        # because 'raw_digits' group excludes the 00/+ already.

        found_phones.append(normalized)

    # Remove duplicates
    found_phones = list(set(found_phones))

    # 2. Extract Order Code
    order_code = None
    code_match = re.search(r'---\s*(\d{5})\s*---', folder_name)
    if code_match:
        order_code = code_match.group(1)

    # 3. Extract Source & Name
    # Structure: Phone --- Code --- ID --- Source --- Name --- City
    parts = re.split(r'\s*-{2,3}\s*', folder_name)

    source = "Unknown"
    customer_name = "Unknown"

    if len(parts) >= 4:
        source = parts[3].strip()
    if len(parts) >= 5:
        customer_name = parts[4].strip()

    return {
        "folder_name": folder_name,
        "phones": found_phones,  # List of all candidates
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
    except dropbox.exceptions.ApiError:
        return []
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

    # We collect ALL candidate phones from ALL folders to check in one DB query
    all_candidate_phones = set()

    # 2. Parse Data
    for name in folder_names:
        data = parse_folder_data(name)
        if data["phones"]:
            parsed_folders.append(data)
            # Add last 10 digits of each candidate to check list
            for p in data["phones"]:
                all_candidate_phones.add(p[-10:])
        else:
            unparsed_folders.append(name)

    # 3. Check Database
    responded_short_numbers = set()

    if all_candidate_phones:
        conn = get_conn()
        cur = conn.cursor()

        check_list = list(all_candidate_phones)
        format_strings = ','.join(['%s'] * len(check_list))

        # Check if ANY of these numbers exist in our messages table
        query = f"""
            SELECT DISTINCT RIGHT(user_phone, 10) as short_phone
            FROM messages
            WHERE sender = 'customer'
            AND RIGHT(user_phone, 10) IN ({format_strings})
        """

        cur.execute(query, tuple(check_list))
        rows = cur.fetchall()

        if rows:
            if isinstance(rows[0], dict):
                responded_short_numbers = {row['short_phone'] for row in rows}
            else:
                responded_short_numbers = {row[0] for row in rows}

        cur.close()
        conn.close()

    # 4. Match & Separate Results
    users_responded = []
    users_no_response = []

    for item in parsed_folders:
        # Determine which of the candidate phones is the "Real" one
        # Rule: Use the one that is in the DB. If none, use the first one.

        active_phone = item["phones"][0] # Default to first found
        is_responded = False

        for p in item["phones"]:
            if p[-10:] in responded_short_numbers:
                active_phone = p
                is_responded = True
                break

        display_data = {
            "phone": active_phone,
            "order_code": item["order_code"],
            "source": item["source"],
            "customer_name": item["customer_name"]
        }

        if is_responded:
            users_responded.append(display_data)
        else:
            users_no_response.append(display_data)

    # 5. Render HTML
    return render_template(
        "dropbox_orders.html",
        total=len(folder_names),
        responded=users_responded,
        no_response=users_no_response,
        unparsed=unparsed_folders
    )
