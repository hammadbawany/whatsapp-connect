# app.py (trimmed/combined - replace your current app.py with this or merge carefully)
from dotenv import load_dotenv
load_dotenv()
import json
import sys
import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from db import get_conn  # assumes db.get_conn exists and DATABASE_URL set
import tempfile
from flask import Response
import traceback


print("BOOT TOKEN:", os.getenv("WA_TOKEN"))
print("BOOT PHONE:", os.getenv("WA_PHONE"))
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-this")
VERIFY_TOKEN = "lifafay123"
WHATSAPP_TOKEN = os.getenv("WA_TOKEN")

PHONE_NUMBER_ID = os.getenv("WA_PHONE")
WABA_ID = os.getenv("WA_WABA_ID")




def get_active_account_id():
    """
    Returns the ID of the latest connected WhatsApp account.
    """
    conn = get_conn()
    print("get_active_account_id")
    # 🟢 FIX: Explicitly use RealDictCursor so we get a Dictionary
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()

    cur.close()
    conn.close()

    # 🟢 FIX: Access by Name ['id'], because row[0] causes KeyError in DictCursor
    return row['id'] if row else None






# ---------- Auth helpers ----------
def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor) # Use Dict Cursor for consistency
    cur.execute("SELECT id, username, role, last_seen FROM users WHERE id=%s", (session["user_id"],))
    u = cur.fetchone()
    cur.close()
    conn.close()
    return u

'''
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # -----------------------------------------------------
    # 1️⃣ VERIFICATION (META HANDSHAKE)
    # -----------------------------------------------------
    if request.method == "GET":
        print("webhook get:")
        VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "lifafay123")
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    # -----------------------------------------------------
    # 2️⃣ INCOMING EVENTS (POST)
    # -----------------------------------------------------
    try:
        print("webhook post:")

        data = request.get_json(silent=True) or {}
        entry = data.get("entry", [])

        if not entry: return "OK", 200
        changes = entry[0].get("changes", [])
        if not changes: return "OK", 200

        value = changes[0].get("value", {})

        # -------------------------------------------------
        # 3️⃣ IDENTIFY SAAS ACCOUNT (WITH FALLBACK FIX)
        # -------------------------------------------------
        metadata = value.get("metadata", {})
        print("FULL METADATA:", metadata, file=sys.stdout)

        incoming_phone_id = metadata.get("phone_number_id")

        # Diagnostic Log
        print(f"🔹 Webhook Hit! Incoming Phone ID: {incoming_phone_id}", file=sys.stdout)

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        whatsapp_account_id = None

        if incoming_phone_id:
            # A. Try Exact Match
            cur.execute("""
                SELECT id FROM whatsapp_accounts
                WHERE phone_number_id = %s
                ORDER BY id DESC LIMIT 1
            """, (incoming_phone_id,))
            account_row = cur.fetchone()

            if account_row:
                whatsapp_account_id = account_row['id']
                print(f"✅ Exact Match Found: Account ID {whatsapp_account_id}", file=sys.stdout)
            else:
                # B. 🟢 SAFETY FALLBACK: If exact match failed, use the LATEST account
                print(f"⚠️ Mismatch! Phone ID {incoming_phone_id} not in DB. Using Fallback.", file=sys.stdout)
                cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
                fallback_row = cur.fetchone()
                if fallback_row:
                    whatsapp_account_id = fallback_row['id']
                    print(f"🔸 Used Latest Account ID: {whatsapp_account_id}", file=sys.stdout)

        # If we STILL don't have an ID, we log critical error but try to continue (will save as NULL)
        if not whatsapp_account_id:
            print("❌ CRITICAL: No Accounts found in DB. Message will save as NULL.", file=sys.stdout)

        # -------------------------------------------------
        # 4️⃣ CAPTURE CONTACT NAME
        # -------------------------------------------------
        contacts_data = value.get("contacts", [])
        if contacts_data:
            contact = contacts_data[0]
            wa_phone = contact.get("wa_id")
            profile_name = contact.get("profile", {}).get("name")

            if wa_phone and profile_name:
                try:
                    cur.execute("""
                        INSERT INTO contacts (phone, name)
                        VALUES (%s, %s)
                        ON CONFLICT (phone)
                        DO UPDATE SET name = EXCLUDED.name
                    """, (wa_phone, profile_name))
                except Exception as e:
                    print(f"⚠️ Error saving name: {e}")

        # -------------------------------------------------
        # 5️⃣ PROCESS INCOMING MESSAGES
        # -------------------------------------------------
        messages = value.get("messages", [])
        for msg in messages:
            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                # Ensure contact exists
                cur.execute("INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING", (phone,))

                # --- A. TEXT MESSAGES ---
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                    if phone and text:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, text, wa_id))

                # --- B. MEDIA MESSAGES (Image, Audio, Video, Document, Sticker) ---
                elif msg_type in ["image", "video", "audio", "voice", "document", "sticker"]:
                    media_object = msg.get(msg_type, {})
                    media_id = media_object.get("id")
                    caption = media_object.get("caption", "") # Only image/video/doc have captions

                    if phone and media_id:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id, user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, msg_type, media_id, caption, wa_id))

                # --- C. INTERACTIVE (Buttons / Lists / Quick Replies) ---
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    i_type = interactive.get("type")
                    text_response = ""

                    if i_type == "button_reply":
                        text_response = interactive.get("button_reply", {}).get("title")
                    elif i_type == "list_reply":
                        text_response = interactive.get("list_reply", {}).get("title")

                    if text_response:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, text_response, wa_id))

                # --- D. UNKNOWN ---
                else:
                    print(f"Ignored message type: {msg_type}")

            except Exception:
                print("❌ Error processing specific message")
                traceback.print_exc()

        # -------------------------------------------------
        # 6️⃣ PROCESS STATUS UPDATES
        # -------------------------------------------------
        statuses = value.get("statuses", [])
        for s in statuses:
            try:
                wa_id = s.get("id")
                new_status = s.get("status") # sent, delivered, read

                if wa_id and new_status:
                    # 🟢 FIX: Update status purely based on Message ID (globally unique).
                    # Removing the Account ID check here ensures ticks update even if Account ID mapping was fuzzy.
                    cur.execute("""
                        SELECT status FROM messages WHERE whatsapp_id = %s
                    """, (wa_id,))
                    row = cur.fetchone()

                    if row:
                        current_status = row['status']
                        should_update = True

                        # Logic: read > delivered > sent
                        if current_status == 'read': should_update = False
                        elif current_status == 'delivered' and new_status == 'sent': should_update = False

                        if should_update:
                            cur.execute("""
                                UPDATE messages SET status = %s WHERE whatsapp_id = %s
                            """, (new_status, wa_id))
                    else:
                        pass # Message might not be in DB yet

            except Exception:
                print("❌ Error processing status update")
                traceback.print_exc()

        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        print(f"❌ Fatal Webhook Error: {e}", file=sys.stdout)
        traceback.print_exc()
    return "OK", 200
'''
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 1. VERIFICATION
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # 2. INCOMING EVENTS
    try:
        data = request.get_json(silent=True) or {}
        entry = data.get("entry", [])
        if not entry: return "OK", 200

        changes = entry[0].get("changes", [])
        if not changes: return "OK", 200

        value = changes[0].get("value", {})

        # --- DIAGNOSTIC LOGGING ---
        metadata = value.get("metadata", {})
        incoming_phone_id = metadata.get("phone_number_id")
        print(f"🔹 [WEBHOOK] Hit! Phone ID: {incoming_phone_id}", file=sys.stdout)
        sys.stdout.flush()

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 3. IDENTIFY ACCOUNT (With Fallback)
        whatsapp_account_id = None
        if incoming_phone_id:
            cur.execute("SELECT id FROM whatsapp_accounts WHERE phone_number_id = %s", (incoming_phone_id,))
            row = cur.fetchone()
            if row:
                whatsapp_account_id = row['id']
                print(f"✅ Exact Match: ID {whatsapp_account_id}", file=sys.stdout)

        if not whatsapp_account_id:
            print("⚠️ Mismatch! Using Fallback to Latest Account.", file=sys.stdout)
            cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
            fallback = cur.fetchone()
            if fallback: whatsapp_account_id = fallback['id']

        # 4. CAPTURE NAME
        contacts_data = value.get("contacts", [])
        if contacts_data:
            contact = contacts_data[0]
            try:
                cur.execute("""
                    INSERT INTO contacts (phone, name) VALUES (%s, %s)
                    ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
                """, (contact.get("wa_id"), contact.get("profile", {}).get("name")))
            except: pass

        # 5. PROCESS MESSAGES
        messages = value.get("messages", [])
        for msg in messages:
            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                cur.execute("INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING", (phone,))

                # Text
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                    print(f"webhook inset text:")
                    cur.execute("""
                        INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
                        VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                    """, (whatsapp_account_id, phone, text, wa_id))

                # Media
                elif msg_type in ["image", "video", "audio", "voice", "document", "sticker"]:
                    media_obj = msg.get(msg_type, {})
                    print(f"webhook inset img:")
                    cur.execute("""
                        INSERT INTO messages (whatsapp_account_id, user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp)
                        VALUES (%s, %s, 'customer', %s, %s, %s, %s, 'received', NOW())
                    """, (whatsapp_account_id, phone, msg_type, media_obj.get("id"), media_obj.get("caption",""), wa_id))

                # Interactive (Buttons/Quick Replies)
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    i_type = interactive.get("type")
                    resp = ""
                    if i_type == "button_reply": resp = interactive.get("button_reply", {}).get("title")
                    elif i_type == "list_reply": resp = interactive.get("list_reply", {}).get("title")

                    if resp:
                        print(f"webhook inset interactive:")

                        cur.execute("""
                            INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
                            VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, resp, wa_id))

            except Exception as e:
                print(f"❌ Msg Save Error: {e}", file=sys.stdout)
                traceback.print_exc()

        # 6. STATUS UPDATES
        statuses = value.get("statuses", [])
        for s in statuses:
            cur.execute("UPDATE messages SET status = %s WHERE whatsapp_id = %s", (s.get("status"), s.get("id")))

        conn.commit()
        cur.close(); conn.close()
        sys.stdout.flush()

    except Exception as e:
        print(f"❌ Webhook Fatal: {e}", file=sys.stdout)
        traceback.print_exc()
        sys.stdout.flush()

    return "OK", 200



            # ---------- Auth routes ----------
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and check_password_hash(row["password_hash"], password):
            session["user_id"] = row["id"]
            # set last_seen and mark online
            conn = get_conn(); cur = conn.cursor()
            cur.execute("UPDATE users SET last_seen=%s WHERE id=%s", (datetime.utcnow(), row["id"]))
            conn.commit(); cur.close(); conn.close()
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- Presence and typing endpoints ----------
@app.route("/presence/heartbeat", methods=["POST"])
@login_required
def presence_heartbeat():
    user = get_current_user()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET last_seen=%s WHERE id=%s", (datetime.utcnow(), user["id"]))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/typing", methods=["POST"])
@login_required
def typing():
    data = request.get_json()
    phone = data.get("phone")
    typing = data.get("typing", False)
    # For simplicity we store typing state in-memory dict (per-process)
    # If you run multiple dynos use Redis to share typing state.
    if "typing_states" not in app.config:
        app.config["typing_states"] = {}
    app.config["typing_states"][phone] = {"user": session["user_id"], "typing": typing, "at": datetime.utcnow().isoformat()}
    return jsonify({"ok": True})

# ---------- API endpoints (list, history, send) ----------
@app.route("/list_users")
@login_required
def list_users():
    account_id = get_active_account_id()
    if not account_id: return jsonify([])

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 FIX: Filter specifically by whatsapp_account_id
    query = """
        WITH LastMsg AS (
            SELECT
                user_phone,
                MAX(timestamp) as last_ts,
                COUNT(CASE WHEN status = 'received' AND whatsapp_account_id = %s THEN 1 END) as unread_count
            FROM messages
            WHERE whatsapp_account_id = %s
            GROUP BY user_phone
        )
        SELECT
            c.phone,
            c.name,
            lm.last_ts,
            COALESCE(lm.unread_count, 0) as unread_count,
            m.message as last_message,
            m.media_type
        FROM contacts c
        INNER JOIN LastMsg lm ON c.phone = lm.user_phone
        LEFT JOIN messages m ON lm.user_phone = m.user_phone AND lm.last_ts = m.timestamp
        ORDER BY lm.last_ts DESC
    """
    cur.execute(query, (account_id, account_id))
    rows = cur.fetchall()
    cur.close(); conn.close()

    for r in rows:
        if r['last_ts']:
            r['last_ts'] = r['last_ts'].isoformat()
            if not r['last_ts'].endswith("Z"): r['last_ts'] += "Z" # Timezone fix

    return jsonify(rows)



@app.route("/history")
@login_required
def history():
    phone = request.args.get("phone")
    account_id = get_active_account_id()

    if not phone or not account_id: return jsonify([])

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 FIX: Strict filter by account_id so we only see relevant msgs
    cur.execute("""
        SELECT sender, message, media_type, media_id, status, timestamp
        FROM messages
        WHERE user_phone = %s AND whatsapp_account_id = %s
        ORDER BY timestamp ASC
    """, (phone, account_id))

    rows = cur.fetchall()
    cur.close(); conn.close()

    results = []
    for r in rows:
        ts = r["timestamp"].isoformat() if r["timestamp"] else ""
        if ts and not ts.endswith("Z"): ts += "Z" # Timezone fix
        r["timestamp"] = ts
        results.append(r)

    return jsonify(results)


@app.route("/send_text", methods=["POST"])
def send_text():
    data = request.json
    phone = data.get("phone")
    text = data.get("text")

    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 Get Credentials for the LATEST Active Account
    cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
    account = cur.fetchone()

    if not account:
        cur.close(); conn.close()
        return jsonify({"error": "No account connected"}), 400

    url = f"https://graph.facebook.com/v20.0/{account['phone_number_id']}/messages"
    headers = {"Authorization": f"Bearer {account['access_token']}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}

    try:
        resp = requests.post(url, json=payload, headers=headers)
        wa_id = resp.json().get("messages", [{}])[0].get("id")
        print("sned_text")
        # 🟢 CRITICAL: Save using account['id'] so it matches the /history endpoint
        cur.execute("""
            INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
            VALUES (%s, %s, 'agent', %s, %s, 'sent', NOW())
        """, (account['id'], phone, text, wa_id))

        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close(); conn.close()


@app.route("/send_media", methods=["POST"])
@login_required
def send_media():
    data = request.json
    phone = data["phone"]
    image_url = data["url"]
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {"messaging_product": "whatsapp","to": phone,"type":"image","image":{"link": image_url}}
    r = requests.post(url, json=payload, headers=headers)
    resp = r.json()
    # Save message record
    wa_id = None
    if isinstance(resp, dict) and resp.get("messages"):
        wa_id = resp["messages"][0].get("id")
    conn = get_conn(); cur = conn.cursor()
    print("send_media")
    cur.execute("""
        INSERT INTO messages (user_phone, sender, message, media_url, timestamp, whatsapp_id, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (phone, "agent", None, image_url, datetime.utcnow(), wa_id, "sent"))
    conn.commit(); cur.close(); conn.close()
    return jsonify(resp)

# ---------- simple endpoint for presence/agents list ----------
@app.route("/agents")
@login_required
def agents():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, username, last_seen, role FROM users WHERE role='agent'")
    rows = cur.fetchall(); cur.close(); conn.close()
    # compute online (last_seen within 40s)
    agents = []
    for r in rows:
        last = r["last_seen"]
        online = False
        if last:
            online = (datetime.utcnow() - last) < timedelta(seconds=40)
        agents.append({"id": r["id"], "username": r["username"], "online": online})
    return jsonify(agents)

# ---------- inbox page ----------
@app.route("/inbox")
@login_required
def inbox():
    return render_template("inbox.html")

if __name__ == "__main__":
    app.run(debug=True)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            return render_template("register.html", error="Please fill all fields.")

        password_hash = generate_password_hash(password)

        conn = get_conn()
        cur = conn.cursor()

        try:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (username, password_hash)
            )
            conn.commit()
            cur.close()
            conn.close()
            return redirect(url_for("login"))

        except Exception as e:
            print("Registration error:", e)
            cur.close()
            conn.close()
            return render_template("register.html", error="Username already exists.")

    return render_template("register.html")


@app.route("/debug_users")
def debug_users():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='users'")
    cols = cur.fetchall()
    cur.close(); conn.close()
    return jsonify(cols)

def upload_media_to_whatsapp(file_path):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}"
    }

    files = {
        "file": open(file_path, "rb"),
        "type": (None, "image/png"),
        "messaging_product": (None, "whatsapp")
    }

    response = requests.post(url, headers=headers, files=files)
    data = response.json()

    if "id" not in data:
        raise Exception(f"Media upload failed: {data}")

    return data["id"]  # media_id

def send_whatsapp_image(phone, media_id, caption=None):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {
            "id": media_id
        }
    }

    if caption:
        payload["image"]["caption"] = caption

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        raise Exception(f"Send image failed: {response.text}")

@app.route("/send_design", methods=["POST"])
def send_design():
    try:
        phone = request.form.get("phone")
        caption = request.form.get("caption", "")
        whatsapp_account_id = request.form.get("whatsapp_account_id")
        file = request.files.get("file")

        print("---- SEND DESIGN START ----")
        print("PHONE:", phone)
        print("CAPTION:", caption)
        print("FILE:", file)

        if not phone or not file or not whatsapp_account_id:
            return jsonify({"error": "missing data"}), 400

        # -------------------------------------------------
        # 1️⃣ Fetch phone_number_id + token for this account
        # -------------------------------------------------
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT phone_number_id, access_token
            FROM whatsapp_accounts
            WHERE id = %s
        """, (whatsapp_account_id,))

        account = cur.fetchone()
        if not account:
            cur.close()
            conn.close()
            return jsonify({"error": "invalid whatsapp account"}), 400

        phone_number_id = account["phone_number_id"]
        token = account["access_token"]

        # -------------------------------------------------
        # 2️⃣ Save file temporarily
        # -------------------------------------------------
        import tempfile, os

        suffix = os.path.splitext(file.filename)[1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        file.save(tmp.name)
        tmp.close()

        print("TEMP FILE SAVED:", tmp.name)

        # -------------------------------------------------
        # 3️⃣ Upload media to WhatsApp
        # -------------------------------------------------
        account = get_account_by_id(whatsapp_account_id)
        phone_number_id, token = account

        upload_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/media"
#        Authorization: f"Bearer {token}"

        upload_headers = {
            "Authorization": f"Bearer {token}"
        }

        upload_data = {
            "messaging_product": "whatsapp",
            "type": file.mimetype
        }

        with open(tmp.name, "rb") as f:
            files = {
                "file": (file.filename, f, file.mimetype)
            }
            upload_resp = requests.post(
                upload_url,
                headers=upload_headers,
                data=upload_data,
                files=files
            )

        upload_json = upload_resp.json()
        print("UPLOAD RESPONSE:", upload_json)

        if upload_resp.status_code != 200 or "id" not in upload_json:
            os.unlink(tmp.name)
            return jsonify({"error": "media upload failed", "details": upload_json}), 500

        media_id = upload_json["id"]

        # -------------------------------------------------
        # 4️⃣ Send media message
        # -------------------------------------------------
        message_url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "image",
            "image": {
                "id": media_id,
                "caption": caption
            }
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        send_resp = requests.post(message_url, json=payload, headers=headers)
        send_json = send_resp.json()

        print("MESSAGE RESPONSE:", send_json)

        wa_id = None
        try:
            wa_id = send_json["messages"][0]["id"]
        except Exception:
            print("⚠️ Could not extract wa_id")

        # -------------------------------------------------
        # 5️⃣ Save outbound message
        # -------------------------------------------------
        print("send_design")
        cur.execute("""
            INSERT INTO messages (
                whatsapp_account_id,
                user_phone,
                sender,
                media_type,
                media_id,
                message,
                whatsapp_id,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            whatsapp_account_id,
            phone,
            "agent",
            "image",
            media_id,
            caption,
            wa_id,
            "sent"
        ))

        conn.commit()
        cur.close()
        conn.close()

        os.unlink(tmp.name)

        print("---- SEND DESIGN SUCCESS ----")
        return jsonify({"success": True})

    except Exception as e:
        print("❌ SEND DESIGN ERROR")
        traceback.print_exc()
        return jsonify({"error": "internal error"}), 500


@app.route("/media/<media_id>")
def get_media(media_id):
    try:
        content = download_whatsapp_media(media_id)
        return Response(content, mimetype="image/jpeg")
    except Exception as e:
        print("Media fetch error:", e)
        return "", 404


def download_whatsapp_media(media_id):
    """
    Downloads media from WhatsApp Cloud API and
    returns raw bytes + content-type
    """

    # Step 1: Get media URL
    meta_url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}"
    }

    meta_resp = requests.get(meta_url, headers=headers)
    meta_json = meta_resp.json()

    if "url" not in meta_json:
        raise Exception(f"Failed to get media URL: {meta_json}")

    media_url = meta_json["url"]

    # Step 2: Download actual media
    media_resp = requests.get(media_url, headers=headers)

    content_type = media_resp.headers.get("Content-Type", "image/jpeg")

    return media_resp.content, content_type

# 2. Universal Attachment Handler (Images, Video, Audio, Docs)
@app.route("/send_attachment", methods=["POST"])
def send_attachment():
    try:
        phone = request.form.get("phone")
        caption = request.form.get("caption", "")
        file = request.files.get("file")
        msg_type = request.form.get("type", "image") # image, video, audio, document

        if not phone or not file: return jsonify({"error": "missing data"}), 400

        # 1. Upload to Meta
        upload_url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

        # WhatsApp requires the 'file' param, and 'messaging_product'
        files_payload = {"file": (file.filename, file.stream, file.mimetype)}
        data_payload = {"messaging_product": "whatsapp"}

        print(f"Uploading {msg_type}...")
        up_resp = requests.post(upload_url, headers=headers, files=files_payload, data=data_payload).json()

        if "id" not in up_resp:
            print("Meta Upload Error:", up_resp)
            return jsonify(up_resp), 500

        media_id = up_resp["id"]

        # 2. Send Message via Meta
        msg_url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

        # Construct payload based on type
        message_body = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": msg_type,
            msg_type: {"id": media_id}
        }

        # Captions are only valid for image, video, document
        if msg_type in ['image', 'video', 'document'] and caption:
            message_body[msg_type]["caption"] = caption

        send_resp = requests.post(msg_url, json=message_body, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()

        # 3. Save to DB
        wa_id = send_resp.get("messages", [{}])[0].get("id")

        conn = get_conn(); cur = conn.cursor()
        print("send_attachment")

        cur.execute("""
            INSERT INTO messages (user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp)
            VALUES (%s, 'agent', %s, %s, %s, %s, 'sent', NOW())
        """, (phone, msg_type, media_id, caption, wa_id))
        conn.commit(); cur.close(); conn.close()

        return jsonify({"success": True})

    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Failed"}), 500



# 3. Add Tag (For Filtering)
@app.route("/set_tag", methods=["POST"])
def set_tag():
    data = request.json
    phone = data.get("phone")
    tag = data.get("tag") # 'unread', 'lead', etc.

    conn = get_conn(); cur = conn.cursor()
    # Upsert tag
    cur.execute("""
        INSERT INTO user_tags (user_phone, tag) VALUES (%s, %s)
        ON CONFLICT (user_phone) DO UPDATE SET tag = %s
    """, (phone, tag, tag))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"success": True})

# 2. CREATE NEW CHAT (Adds to contacts table)
@app.route("/create_contact", methods=["POST"])
@login_required
def create_contact():
    data = request.json
    phone = data.get("phone")

    if not phone: return jsonify({"error": "No phone"}), 400

    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING", (phone,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(e)
        return jsonify({"error": "Failed"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/mark_read", methods=["POST"])
def mark_read():
    try:
        data = request.json
        phone = data.get("phone")

        conn = get_conn()
        cur = conn.cursor()

        # Update all 'received' messages from this phone to 'read'
        # This clears the count in the database
        cur.execute("""
            UPDATE messages
            SET status = 'read'
            WHERE user_phone = %s AND status = 'received'
        """, (phone,))

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("Mark read error:", e)
        return jsonify({"error": "failed"}), 500


@app.route("/me")
@login_required
def me():
    # Helper 'get_current_user' must be defined in your app.py
    # (It was in the original code I provided)
    user = get_current_user()
    return jsonify({
        "username": user["username"] if user else "Agent",
        "id": user["id"] if user else 0
    })


@app.route("/get_templates")
@login_required
def get_templates():
    if not WABA_ID:
        return jsonify({"error": "WABA_ID missing in .env"}), 500

    url = f"https://graph.facebook.com/v20.0/{WABA_ID}/message_templates"
    params = {
        "status": "APPROVED",
        "limit": 100
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    try:
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()
        return jsonify(data)
    except Exception as e:
        print("Template Fetch Error:", e)
        return jsonify({"error": str(e)}), 500

# ==========================================
# 🟢 NEW: SEND TEMPLATE MESSAGE
# ==========================================
@app.route("/send_template", methods=["POST"])
def send_template():
    data = request.json
    phone = data.get("phone")
    template_name = data.get("template_name")
    language = data.get("language", "en_US")
    # 🟢 Get the actual text passed from frontend, default to placeholder if missing
    template_body = data.get("template_body", f"[Template: {template_name}]")

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": language
            }
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=payload)
        json_resp = resp.json()

        if "messages" in json_resp:
            wa_id = json_resp["messages"][0]["id"]
            conn = get_conn(); cur = conn.cursor()
            print("send_template")

            # 🟢 Save the ACTUAL template text to the database
            cur.execute("""
                INSERT INTO messages (user_phone, sender, message, whatsapp_id, status, timestamp)
                VALUES (%s, 'agent', %s, %s, 'sent', NOW())
            """, (phone, template_body, wa_id))

            conn.commit(); cur.close(); conn.close()
            return jsonify({"success": True})
        else:
            print("Meta Error:", json_resp)
            return jsonify(json_resp), 400

    except Exception as e:
        print("Send Template Error:", e)
        return jsonify({"error": str(e)}), 500


# ==========================================
# 🟢 QUICK REPLIES ROUTES
# ==========================================

@app.route("/quick_replies", methods=["GET"])
def get_quick_replies():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM quick_replies ORDER BY shortcut")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify(rows)

@app.route("/quick_replies", methods=["POST"])
def add_quick_reply():
    try:
        data = request.json
        if not data:
            print("❌ Error: No JSON data received")
            return jsonify({"error": "No data"}), 400

        # Safely get and clean data
        raw_shortcut = data.get("shortcut", "")
        shortcut = raw_shortcut.replace("/", "").strip() if raw_shortcut else ""
        message = data.get("message", "").strip()

        print(f"DEBUG: Adding -> Shortcut: '{shortcut}' | Message Len: {len(message)}")

        # Validation
        if not shortcut:
            return jsonify({"error": "Shortcut is required (e.g. 'intro')"}), 400
        if not message:
            return jsonify({"error": "Message cannot be empty"}), 400

        # Database Insert
        conn = get_conn()
        cur = conn.cursor()

        # Check for duplicate first
        cur.execute("SELECT id FROM quick_replies WHERE shortcut = %s", (shortcut,))
        if cur.fetchone():
            cur.close(); conn.close()
            print(f"❌ Error: Shortcut '{shortcut}' already exists")
            return jsonify({"error": f"Shortcut '{shortcut}' already exists"}), 400

        cur.execute("INSERT INTO quick_replies (shortcut, message) VALUES (%s, %s)", (shortcut, message))
        conn.commit()
        cur.close()
        conn.close()

        print("✅ Quick reply saved!")
        return jsonify({"success": True})

    except Exception as e:
        print("❌ SERVER ERROR in add_quick_reply:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/quick_replies/<int:id>", methods=["DELETE"])
def delete_quick_reply(id):
    try:
        print(f"DEBUG: Deleting Quick Reply ID: {id}")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM quick_replies WHERE id = %s", (id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("❌ Delete Error:", e)
        return jsonify({"error": "Failed to delete"}), 500

@app.route("/quick_replies/<int:id>", methods=["PUT"])
def update_quick_reply(id):
    try:
        data = request.json
        raw_shortcut = data.get("shortcut", "")
        shortcut = raw_shortcut.replace("/", "").strip() if raw_shortcut else ""
        message = data.get("message", "").strip()

        print(f"DEBUG: Updating ID {id} -> Shortcut: '{shortcut}'")

        if not shortcut or not message:
            return jsonify({"error": "Missing shortcut or message"}), 400

        conn = get_conn()
        cur = conn.cursor()

        # Update
        cur.execute("UPDATE quick_replies SET shortcut = %s, message = %s WHERE id = %s", (shortcut, message, id))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"success": True})

    except Exception as e:
        print("❌ Update Error:", e)
        # Check if it was a duplicate key error
        if "unique constraint" in str(e).lower():
             return jsonify({"error": "Shortcut name already taken"}), 400
        return jsonify({"error": "Failed to update"}), 500

@app.route("/whatsapp/connect")
@login_required
def whatsapp_connect():
    fb_app_id = os.getenv("FB_APP_ID")
    redirect_uri = url_for("whatsapp_callback", _external=True, _scheme='https')

    state = "some_random_security_string"
    session["oauth_state"] = state

    # 🟢 REMOVED 'business_management' -> It causes the error
    scope = "whatsapp_business_management,whatsapp_business_messaging"

    url = (
        "https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={fb_app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
        f"&scope={scope}"
        f"&response_type=code"
    )

    return redirect(url)

@app.route("/whatsapp/callback")
@login_required
def whatsapp_callback():
    try:
        # 1. Check for Error from Facebook
        if request.args.get("error"):
            return f"Facebook Error: {request.args.get('error_description')}", 400

        # 2. Get Code
        code = request.args.get("code")
        if not code:
            return "Error: No code received from Facebook.", 400

        # 3. Exchange Code for Token
        fb_app_id = os.getenv("FB_APP_ID")
        fb_app_secret = os.getenv("FB_APP_SECRET")
        # Ensure this matches your Connect URL exactly (https vs http)
        redirect_uri = url_for("whatsapp_callback", _external=True, _scheme='https')

        token_url = (
            f"https://graph.facebook.com/v19.0/oauth/access_token"
            f"?client_id={fb_app_id}"
            f"&client_secret={fb_app_secret}"
            f"&redirect_uri={redirect_uri}"
            f"&code={code}"
        )

        resp = requests.get(token_url)
        token_data = resp.json()

        if "error" in token_data:
            return jsonify(token_data), 400

        access_token = token_data["access_token"]

        # 4. Run Setup (with error catching)
        return setup_whatsapp_business(access_token)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"CRITICAL ERROR: {str(e)}", 500

def setup_whatsapp_business(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    fb_app_id = os.getenv("FB_APP_ID")
    fb_app_secret = os.getenv("FB_APP_SECRET")

    try:
        print("DEBUG: Inspecting Token for WABA IDs...")

        # 1️⃣ Debug token to extract granular scopes
        debug_url = (
            f"https://graph.facebook.com/v20.0/debug_token"
            f"?input_token={access_token}"
            f"&access_token={fb_app_id}|{fb_app_secret}"
        )

        debug_resp = requests.get(debug_url).json()
        print("DEBUG TOKEN RESP:", debug_resp)

        if "error" in debug_resp:
            return f"Token Error: {debug_resp['error']['message']}", 400

        data = debug_resp.get("data", {})
        granular_scopes = data.get("granular_scopes", [])

        # 2️⃣ Collect ALL WABA IDs
        waba_ids = []
        for scope_obj in granular_scopes:
            if scope_obj.get("scope") == "whatsapp_business_management":
                waba_ids.extend(scope_obj.get("target_ids", []))

        print("DEBUG: All WABA IDs from token:", waba_ids)

        if not waba_ids:
            return (
                "No WhatsApp Business Account selected during login. "
                "Please retry and select a business.",
                400,
            )

        # 3️⃣ Find the WABA that actually has a phone number
        selected_waba = None
        phone_number_id = None
        display_phone = None

        for waba in waba_ids:
            phone_resp = requests.get(
                f"https://graph.facebook.com/v20.0/{waba}/phone_numbers",
                headers=headers
            ).json()

            print(f"DEBUG phone_numbers for WABA {waba}:", phone_resp)

            if phone_resp.get("data"):
                selected_waba = waba
                phone_data = phone_resp["data"][0]
                phone_number_id = phone_data["id"]
                display_phone = phone_data["display_phone_number"]
                break

        if not selected_waba:
            return (
                "WhatsApp Business Account found, but no phone numbers are attached. "
                "Please add a number in WhatsApp Manager.",
                400,
            )

        print("✅ SELECTED WABA:", selected_waba)
        print("✅ PHONE NUMBER ID:", phone_number_id)
        print("✅ DISPLAY PHONE:", display_phone)

        # 4️⃣ Subscribe app to webhooks
        sub_url = f"https://graph.facebook.com/v20.0/{selected_waba}/subscribed_apps"
        requests.post(sub_url, headers=headers)

        # 5️⃣ SAVE SYSTEM USER TOKEN (NOT user token)
        system_token = os.getenv("WA_SYSTEM_TOKEN")
        if not system_token:
            return "System token not configured (WA_SYSTEM_TOKEN missing)", 500

        save_whatsapp_account(
            selected_waba,
            phone_number_id,
            display_phone,
            system_token
        )

        print("✅ WhatsApp business setup completed successfully")
        return redirect("/")

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Setup Logic Failed: {str(e)}", 500

'''
@app.route("/whatsapp/fetch-assets")
def fetch_assets():
    token = session["fb_token"]

    wabas = requests.get(
        "https://graph.facebook.com/v19.0/me/whatsapp_business_accounts",
        headers={"Authorization": f"Bearer {token}"}
    ).json()
    print("DEBUG WABA RESPONSE:", wabas)

    waba_id = wabas["data"][0]["id"]

    phones = requests.get(
        f"https://graph.facebook.com/v19.0/{waba_id}/phone_numbers",
        headers={"Authorization": f"Bearer {token}"}
    ).json()

    phone = phones["data"][0]

    save_whatsapp_account(
        waba_id=waba_id,
        phone_number_id=phone["id"],
        phone_display=phone["display_phone_number"],
        token=token
    )
    # Save these in DB
    #save_to_db(waba_id, phone_number_id, token)

    return redirect("/inbox")
'''

def save_whatsapp_account(waba_id, phone_number_id, display_phone, token):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO whatsapp_accounts (waba_id, phone_number_id, display_phone, access_token)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (phone_number_id)
            DO UPDATE SET access_token = EXCLUDED.access_token, display_phone = EXCLUDED.display_phone
        """, (waba_id, phone_number_id, display_phone, token))
        conn.commit()
    except Exception as e:
        print("DB Error:", e)
    finally:
        cur.close()
        conn.close()


def get_account_by_phone_id(phone_number_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, access_token
        FROM whatsapp_accounts
        WHERE phone_number_id = %s
    """, (phone_number_id,))

    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return None

    return {
        "account_id": row[0],
        "access_token": row[1]
    }


def save_message(account_id, phone, sender, message, wamid):
    conn = get_conn()
    cur = conn.cursor()
    print("save_message")

    cur.execute("""
        INSERT INTO messages (whatsapp_account_id, phone, sender, message, wamid)
        VALUES (%s, %s, %s, %s, %s)
    """, (account_id, phone, sender, message, wamid))

    conn.commit()
    cur.close()
    conn.close()

def get_whatsapp_account_id(phone_number_id):
    conn = get_conn()
    cur = conn.cursor()
    # 🟢 FIX: If duplicates exist, pick the LATEST one to match Frontend logic
    cur.execute("""
        SELECT id
        FROM whatsapp_accounts
        WHERE phone_number_id = %s
        ORDER BY id DESC
        LIMIT 1
    """, (phone_number_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_account_context(whatsapp_account_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT phone_number_id, access_token
        FROM whatsapp_accounts
        WHERE id = %s
    """, (whatsapp_account_id,))

    row = cur.fetchone()
    cur.close()
    conn.close()

    return row


@app.route("/")
def index():
    # 1. Not Logged In? -> Login
    if "user_id" not in session:
        return redirect(url_for("login"))

    # 2. Logged In? Check for WhatsApp Connection
    conn = get_conn()
    cur = conn.cursor()
    # Check if we have at least one account linked
    cur.execute("SELECT id FROM whatsapp_accounts LIMIT 1")
    account = cur.fetchone()
    cur.close()
    conn.close()

    if account:
        # 3. Connected -> Go to Inbox
        return redirect(url_for("inbox"))
    else:
        # 4. Not Connected -> Go to Onboarding
        return redirect(url_for("connect_page"))

@app.route("/connect")
@login_required
def connect_page():
    # If they somehow get here but are already connected, push to inbox
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM whatsapp_accounts LIMIT 1")
    if cur.fetchone():
        cur.close(); conn.close()
        return redirect(url_for("inbox"))

    cur.close(); conn.close()
    return render_template("connect.html")
