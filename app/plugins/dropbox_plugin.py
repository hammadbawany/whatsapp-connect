import os
import requests
import dropbox
import urllib.parse
import re
from flask import Blueprint, request, redirect, session, jsonify, render_template
from app.db import get_conn
from datetime import datetime, timedelta
dropbox_bp = Blueprint("dropbox", __name__)
import tempfile
import mimetypes
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import DictCursor

# --- CONFIGURATION ---
APP_KEY = os.getenv("DROPBOX_APP_KEY")
APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
REDIRECT_URI = os.getenv("DROPBOX_REDIRECT_URI")

assert APP_KEY, "DROPBOX_APP_KEY missing"
assert APP_SECRET, "DROPBOX_APP_SECRET missing"
assert REDIRECT_URI, "DROPBOX_REDIRECT_URI missing"


EXTERNAL_DB_URL = "postgresql://u8crgmufmb2vp9:p9eb3995b1650b4c908cb98ca407a7300106031951989ff30824c937b828fdeb9@ec2-3-214-33-144.compute-1.amazonaws.com/d388972uunvvuc"

def get_remote_order_details(missing_codes):
    print(f"\n[REMOTE DB] 🔌 Connecting to fetch {len(missing_codes)} codes...")
    print(f"[REMOTE DB] Codes to find: {missing_codes}")

    fetched_data = {}
    remote_conn = None

    try:
        remote_conn = psycopg2.connect(EXTERNAL_DB_URL)
        cur = remote_conn.cursor(cursor_factory=DictCursor)

        format_strings = ','.join(['%s'] * len(missing_codes))

        # NOTE: Ensure table name is 'orders' and column is 'order_id'
        query = f"""
            SELECT order_id, customer_phone, customer_address, customer_city
            FROM orders
            WHERE CAST(order_id AS TEXT) IN ({format_strings})
        """

        cur.execute(query, tuple(missing_codes))
        rows = cur.fetchall()

        print(f"[REMOTE DB] ✅ Success! Found {len(rows)} records.")

        for row in rows:
            # Handle both dict and tuple returns just in case
            if isinstance(row, dict):
                code = str(row['order_id'])
                data = {
                    'phone': row['customer_phone'],
                    'address': row['customer_address'],
                    'city': row['customer_city']
                }
            else:
                code = str(row[0])
                data = {'phone': row[1], 'address': row[2], 'city': row[3]}

            fetched_data[code] = data
            print(f"   -> Found Code {code}: {data['city']}")

        cur.close()

    except Exception as e:
        print(f"[REMOTE DB] ❌ CONNECTION ERROR: {e}")
    finally:
        if remote_conn:
            remote_conn.close()

    return fetched_data

def sync_order_details(order_codes):
    """
    1. Checks LOCAL cache.
    2. Fetches MISSING from REMOTE DB.
    3. Updates LOCAL cache.
    4. Returns dictionary {code: {'address':..., 'city':..., 'phone':...}}
    """
    # Filter out None/Empty codes
    valid_codes = [str(c) for c in order_codes if c]
    if not valid_codes: return {}

    conn = get_conn() # Your local DB connection
    cur = conn.cursor()

    # 1. Check Local Cache
    format_strings = ','.join(['%s'] * len(valid_codes))
    query = f"""
        SELECT order_code, customer_phone, customer_address, customer_city
        FROM cached_order_info
        WHERE order_code IN ({format_strings})
    """
    cur.execute(query, tuple(valid_codes))

    local_results = {}
    found_codes = set()

    for row in cur.fetchall():
        # Handle tuple or dict return depending on your get_conn configuration
        if isinstance(row, dict):
            code = row['order_code']
            data = {'phone': row['customer_phone'], 'address': row['customer_address'], 'city': row['customer_city']}
        else:
            code = row[0]
            data = {'phone': row[1], 'address': row[2], 'city': row[3]}

        local_results[code] = data
        found_codes.add(code)

    # 2. Identify Missing
    missing_codes = [c for c in valid_codes if c not in found_codes]

    # 3. Fetch Remote (Only if needed)
    if missing_codes:
        print(f"Fetching {len(missing_codes)} orders from Remote External DB...")
        new_data = get_remote_order_details(missing_codes)

        if new_data:
            insert_values = []
            for code, info in new_data.items():
                # Add to results for immediate return
                local_results[code] = info
                # Prepare for DB Insert
                insert_values.append((code, info.get('phone'), info.get('address'), info.get('city')))

            # 4. Save to Local Cache (Upsert)
            if insert_values:
                upsert_query = """
                    INSERT INTO cached_order_info (order_code, customer_phone, customer_address, customer_city)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (order_code) DO UPDATE SET
                        customer_phone = EXCLUDED.customer_phone,
                        customer_address = EXCLUDED.customer_address,
                        customer_city = EXCLUDED.customer_city,
                        updated_at = NOW()
                """
                try:
                    cur.executemany(upsert_query, insert_values)
                    conn.commit()
                except Exception as e:
                    print(f"Cache Insert Error: {e}")
                    conn.rollback()

    cur.close()
    conn.close()

    return local_results


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
    base_path = "/1 daniyal/Auto"

    # 1. Get Folders from Dropbox
    folder_names = get_all_dropbox_folders(dbx, base_path)

    parsed_folders = []
    unparsed_folders = []
    all_candidate_phones = set()
    all_codes = set()

    # --- PARSING LOOP ---
    for name in folder_names:
        data = parse_folder_data(name)
        data["full_path"] = f"{base_path}/{name}"

        # Backup: Extract city from folder name (if DB lookup fails later)
        parts = name.split("--")
        if len(parts) > 1:
            data['folder_city'] = parts[-1].strip()
        else:
            data['folder_city'] = "Unknown"

        if data["order_code"]:
            all_codes.add(data["order_code"])

        if data["phones"]:
            parsed_folders.append(data)
            for p in data["phones"]: all_candidate_phones.add(p[-10:])
        else:
            data["name"] = name
            unparsed_folders.append(data)

    # --- NEW: SYNC ADDRESS/CITY/PHONE FROM DB ---
    # This calls the helper function defined above
    all_codes_list = list(all_codes)
    cached_db_info = sync_order_details(all_codes_list)

    # 2. Check Local Database (Messages & Sent Log)
    responded_short_numbers = set()
    sent_folders_set = set()
    call_log_map = {}

    conn = get_conn()
    cur = conn.cursor()

    # A. Check Messages (WhatsApp Replies)
    if all_candidate_phones:
        check_list = list(all_candidate_phones)
        format_strings = ','.join(['%s'] * len(check_list))
        query = f"SELECT DISTINCT RIGHT(user_phone, 10) as short_phone FROM messages WHERE sender = 'customer' AND RIGHT(user_phone, 10) IN ({format_strings})"
        try:
            cur.execute(query, tuple(check_list))
            rows = cur.fetchall()
            for row in rows:
                val = row['short_phone'] if isinstance(row, dict) else row[0]
                if val: responded_short_numbers.add(val)
        except Exception as e:
            print(f"Msg Check Error: {e}")
            conn.rollback()

    # B. Fetch Sent Log
    try:
        cur.execute("SELECT folder_name FROM design_sent_log")
        rows = cur.fetchall()
        for row in rows:
            val = row['folder_name'] if isinstance(row, dict) else row[0]
            sent_folders_set.add(val)
    except:
        conn.rollback()

    # C. Fetch Call Logs
    if all_codes:
        codes_list = list(all_codes)
        fmt = ','.join(['%s'] * len(codes_list))
        try:
            cur.execute(f"""
                SELECT order_code, status, outcome, created_at
                FROM call_logs
                WHERE order_code IN ({fmt})
                ORDER BY created_at ASC
            """, tuple(codes_list))

            for row in cur.fetchall():
                c_code = row['order_code'] if isinstance(row, dict) else row[0]
                c_stat = row['status'] if isinstance(row, dict) else row[1]
                c_out = row['outcome'] if isinstance(row, dict) else row[2]
                c_time = row['created_at'] if isinstance(row, dict) else row[3]

                call_log_map[c_code] = {
                    'status': c_stat,
                    'outcome': c_out,
                    'time': c_time.strftime("%d %b %H:%M") if c_time else ""
                }
        except Exception as e:
            print(f"Call Log Error: {e}")
            conn.rollback()

    cur.close(); conn.close()

    # 3. CATEGORIZE & MERGE DATA
    users_responded = []
    users_no_response = []

    for item in parsed_folders:
        active_phone = item["phones"][0]
        phone_match_in_db = False

        for p in item["phones"]:
            if p[-10:] in responded_short_numbers:
                active_phone = p
                phone_match_in_db = True
                break

        # Data enrichment
        code = item["order_code"]
        call_info = call_log_map.get(code, {})

        # --- MERGE DB INFO ---
        db_details = cached_db_info.get(code, {})

        # 1. Address: DB > 'N/A'
        final_address = db_details.get('address') or "N/A"

        # 2. City: DB > Folder Name > 'Unknown'
        final_city = db_details.get('city')
        if not final_city or final_city.lower() == 'none':
            final_city = item.get('folder_city', 'Unknown')

        # 3. Phone Fallback: If folder has no phone (unlikely here as we looped parsed_folders), use DB
        # This is mostly for your reference, displayed in the table
        db_phone = db_details.get('phone')

        # Call Status String
        last_call_txt = None
        if call_info:
            stat = call_info.get('status', '')
            out = call_info.get('outcome', '')
            if out: stat += f" ({out})"
            last_call_txt = stat

        display_data = {
            "phone": active_phone,
            "db_phone": db_phone, # Passed to template if you want to show alternate phone
            "order_code": code,
            "source": item["source"],
            "customer_name": item["customer_name"],
            "folder_name": item["folder_name"],
            "full_path": item["full_path"],
            "is_sent": item["folder_name"] in sent_folders_set,
            "city": final_city,
            "address": final_address,
            "last_call": last_call_txt,
            "last_call_time": call_info.get('time', ''),
            "call_outcome": call_info.get('outcome', '') # Added for badge logic in HTML
        }

        source_lower = item["source"].lower()
        is_website = "website" in source_lower or "web" in source_lower

        if not is_website:
            users_responded.append(display_data)
        elif is_website and phone_match_in_db:
            users_responded.append(display_data)
        else:
            users_no_response.append(display_data)

    # 👇 ADD THIS SORTING LOGIC 👇
    def sort_key(x):
        # Safely convert order_code to int for correct numerical sorting
        # If code is missing or invalid, treat it as 0
        code = x.get('order_code')
        return int(code) if code and str(code).isdigit() else 0

    users_responded.sort(key=sort_key, reverse=True)
    users_no_response.sort(key=sort_key, reverse=True)
    # 👆 ---------------------- 👆

    # 👇 ADD THIS SORTING LOGIC 👇
    def sort_key(x):
        # Safely convert order_code to int for correct numerical sorting
        # If code is missing or invalid, treat it as 0
        code = x.get('order_code')
        return int(code) if code and str(code).isdigit() else 0

    users_responded.sort(key=sort_key, reverse=True)
    users_no_response.sort(key=sort_key, reverse=True)
    # 👆 ---------------------- 👆

    return render_template(
        "dropbox_orders.html",
        total=len(folder_names),
        responded=users_responded,
        no_response=users_no_response,
        completed=[],
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
        "/1 daniyal/Auto/send to customer/Faraz Corrections",
        "/1 daniyal/Auto/send to customer/no reply",
        "/1 daniyal/Auto/send to customer"
    ]


    # 2. Get Folders with Path Context
    folders_data = {} # Use dict to dedup by folder name

    for path in target_paths:
        current_names = get_all_dropbox_folders(dbx, path)
        parent_name = path.split("/")[-1] # e.g. "Correction done"

        for name in current_names:
            # Skip ignored folders immediately
            if name.lower() in ["instagram", "no reply", "confirm", "file issues", "cancelled orders", "correction done", "faraz corrections"]:
                continue

            folders_data[name] = {
                "name": name,
                "parent": parent_name,
                "full_path": path + "/" + name
            }

    parsed_folders = []
    unparsed_list = []
    all_candidate_phones = set()

    # Iterate over unique folders
    for name, info in folders_data.items():
        data = parse_folder_data(name)

        # Inject Path Info into the parsed data object
        data["parent_folder"] = info["parent"]
        data["full_path"] = info["full_path"]

        if data["phones"]:
            parsed_folders.append(data)
            for p in data["phones"]:
                all_candidate_phones.add(p[-10:])
        else:
            # Add path info to unparsed item too
            unparsed_item = {"name": name, "full_path": info["full_path"], "parent": info["parent"]}
            unparsed_list.append(unparsed_item)

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
            "folder_name": item["folder_name"],
            "full_path": item["full_path"],       # <--- Added
            "parent_folder": item["parent_folder"] # <--- Added
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

# ==========================================
# SYSTEM DROPBOX CLIENT (FOR AUTOMATIONS)
# ==========================================

def get_system_dropbox_client():
    """
    Returns a Dropbox client using SYSTEM credentials.
    Priority 1: SYSTEM_REFRESH_TOKEN (Env Var)
    Priority 2: Database Fallback (Uses the Admin's login session)
    Priority 3: SYSTEM_DROPBOX_TOKEN (Static Env Var - deprecated/fallback)
    """

    # 1. Try Environment Variable Refresh Token (Fastest)
    refresh_token = os.getenv("SYSTEM_REFRESH_TOKEN")

    if refresh_token and APP_KEY and APP_SECRET:
        try:
            dbx = dropbox.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=APP_KEY,
                app_secret=APP_SECRET
            )
            # Verify connectivity
            dbx.users_get_current_account()
            return dbx
        except Exception as e:
            print(f"[SYSTEM DBX] ⚠️ Auto-refresh via Env Var failed: {e}")

    # 2. Database Fallback (Most Reliable for active apps)
    # Fetches the token from the 'dropbox_accounts' table
    try:
        conn = get_conn()
        cur = conn.cursor()
        # Get the most recently active account
        cur.execute("SELECT access_token, refresh_token FROM dropbox_accounts ORDER BY token_updated_at DESC LIMIT 1")
        row = cur.fetchone()

        # If no timestamp, just get any account
        if not row:
            cur.execute("SELECT access_token, refresh_token FROM dropbox_accounts LIMIT 1")
            row = cur.fetchone()

        cur.close()
        conn.close()

        if row:
            # Handle tuple vs dict based on cursor factory
            if isinstance(row, dict):
                db_access = row['access_token']
                db_refresh = row['refresh_token']
            else:
                db_access = row[0]
                db_refresh = row[1]

            # Try constructing with refresh token (preferred)
            if db_refresh:
                print("[SYSTEM DBX] 🔄 Using Database REFRESH token")
                dbx = dropbox.Dropbox(
                    oauth2_refresh_token=db_refresh,
                    app_key=APP_KEY,
                    app_secret=APP_SECRET
                )
                dbx.users_get_current_account()
                return dbx

            # Fallback to Access Token (might be expired, but worth a shot)
            elif db_access:
                print("[SYSTEM DBX] ⚠️ Using Database ACCESS token (No refresh token found)")
                dbx = dropbox.Dropbox(db_access)
                dbx.users_get_current_account()
                return dbx

    except Exception as e:
        print(f"[SYSTEM DBX] ⚠️ Database fallback failed: {e}")


    # 3. Fallback: Static Access Token (Likely expired, but last resort)
    access_token = os.getenv("SYSTEM_DROPBOX_TOKEN")
    if access_token:
        print("[SYSTEM DBX] ⚠️ Falling back to static SYSTEM_DROPBOX_TOKEN")
        dbx = dropbox.Dropbox(access_token)
        try:
            dbx.users_get_current_account()
            return dbx
        except Exception as e:
            print(f"System Dropbox auth failed: {e}")
            raise e

    raise Exception("No valid Dropbox authentication method found (Env or DB).")

# ==========================================
# SVG HELPERS (FOR DESIGN AUTOMATION)
# ==========================================

from io import BytesIO

def download_svg_to_memory(dropbox_path: str) -> BytesIO:
    """
    Downloads an SVG file from Dropbox and returns it as BytesIO
    so it can be parsed by xml.etree.ElementTree
    """
    dbx = get_system_dropbox_client()

    metadata, response = dbx.files_download(dropbox_path)

    return BytesIO(response.content)
