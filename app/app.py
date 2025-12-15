# app.py (trimmed/combined - replace your current app.py with this or merge carefully)
from dotenv import load_dotenv
load_dotenv()


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
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, username, role, last_seen FROM users WHERE id=%s", (session["user_id"],))
    u = cur.fetchone()
    cur.close(); conn.close()
    return u

# ---------- Webhook verification ----------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # -----------------------------------------------------
    # 1️⃣ VERIFICATION (META HANDSHAKE)
    # -----------------------------------------------------
    if request.method == "GET":
        VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "lifafay123")
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    # -----------------------------------------------------
    # 2️⃣ INCOMING EVENTS (MESSAGES & STATUS UPDATES)
    # -----------------------------------------------------
    try:
        data = request.get_json(silent=True) or {}
        entry = data.get("entry", [])

        if not entry:
            return "OK", 200

        changes = entry[0].get("changes", [])
        if not changes:
            return "OK", 200

        # -------------------------------------------------
        # 🆕 IDENTIFY WHICH WHATSAPP NUMBER THIS EVENT IS FOR
        # -------------------------------------------------
        metadata = value.get("metadata", {})
        phone_number_id = metadata.get("phone_number_id")

        whatsapp_account_id = None
        if phone_number_id:
            whatsapp_account_id = get_whatsapp_account_id(phone_number_id)

        if not whatsapp_account_id:
            print(f"⚠️ Unknown phone_number_id: {phone_number_id}")
        # Database Connection
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # =================================================
        # 🟢 NEW: CAPTURE CONTACT NAME
        # =================================================
        # WhatsApp sends the user's profile name here.
        # We save it to the 'contacts' table so the UI shows the name.
        contacts_data = value.get("contacts", [])
        if contacts_data:
            for contact in contacts_data:
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
                        print(f"⚠️ Error saving contact name: {e}")

        # -------------------------------------------------
        # A) INCOMING MESSAGES
        # -------------------------------------------------
        messages = value.get("messages", [])
        for msg in messages:
            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                # Ensure contact exists in DB even if name wasn't sent
                cur.execute("INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING", (phone,))

                # --- TEXT ---
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                    if phone and text:
                        cur.execute("""
                        INSERT INTO messages (
                            whatsapp_account_id,
                            user_phone,
                            sender,
                            message,
                            whatsapp_id,
                            status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        whatsapp_account_id,
                        phone,
                        "customer",
                        text,
                        wa_id,
                        "received"
                    ))


                # --- MEDIA (Image, Audio, Video, Document, Voice) ---
                # 🟢 NEW: Handles all media types dynamically
                elif msg_type in ["image", "video", "audio", "voice", "document", "sticker"]:
                    media_object = msg.get(msg_type, {})
                    media_id = media_object.get("id")
                    caption = media_object.get("caption", "") # Only image/video/doc have captions

                    # Normalize 'voice' to 'audio' for your frontend logic if preferred,
                    # or keep as is.

                    if phone and media_id:
                        cur.execute("""
                            INSERT INTO messages (
                                user_phone, sender, media_type, media_id, message, whatsapp_id, status
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            phone,
                            "customer",
                            msg_type,  # Saves 'image', 'video', 'document', etc.
                            media_id,
                            caption,   # Saves caption if it exists
                            wa_id,
                            "received"
                        ))

                # --- UNKNOWN ---
                else:
                    print(f"Ignored message type: {msg_type}")

            except Exception:
                print("❌ Error processing incoming message")
                traceback.print_exc()

        # -------------------------------------------------
        # B) STATUS UPDATES (Sent -> Delivered -> Read)
        # -------------------------------------------------
        statuses = value.get("statuses", [])
        for s in statuses:
            try:
                wa_id = s.get("id")
                new_status = s.get("status") # sent, delivered, read

                if wa_id and new_status:
                    # Check current status first to prevent overwriting "read" with "delivered"
                    cur.execute("""
                        SELECT status
                        FROM messages
                        WHERE whatsapp_id = %s
                          AND whatsapp_account_id = %s
                    """, (wa_id, whatsapp_account_id))
                    row = cur.fetchone()

                    if row:
                        current_status = row['status']

                        should_update = True
                        if current_status == 'read':
                            should_update = False
                        elif current_status == 'delivered' and new_status == 'sent':
                            should_update = False

                        if should_update:
                            cur.execute("""
                                UPDATE messages
                                SET status = %s
                                WHERE whatsapp_id = %s
                                  AND whatsapp_account_id = %s
                            """, (new_status, wa_id, whatsapp_account_id))
                    else:
                        # Message not in DB yet (Race condition)
                        print(f"⚠️ Status {new_status} received for ID {wa_id}, but message not in DB yet.")

            except Exception:
                print("❌ Error processing status update")
                traceback.print_exc()

        conn.commit()
        cur.close()
        conn.close()

    except Exception:
        print("❌ Fatal Webhook Error")
        traceback.print_exc()

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
            return redirect(url_for("inbox"))
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
def list_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Logic: Get all Contacts, Find their Last Message, Count Unread, Get Tags
    query = """
        WITH LastMsg AS (
            SELECT
                user_phone,
                MAX(timestamp) as last_ts,
                COUNT(CASE WHEN status = 'received' THEN 1 END) as unread_count
            FROM messages
            GROUP BY user_phone
        )
        SELECT
            c.phone,
            lm.last_ts,
            COALESCE(lm.unread_count, 0) as unread_count,
            m.message as last_message,
            m.media_type,
            ct.tag
        FROM contacts c
        LEFT JOIN LastMsg lm ON c.phone = lm.user_phone
        LEFT JOIN messages m ON lm.user_phone = m.user_phone AND lm.last_ts = m.timestamp
        LEFT JOIN contact_tags ct ON c.phone = ct.phone
        ORDER BY lm.last_ts DESC NULLS FIRST
    """

    cur.execute(query)
    rows = cur.fetchall()
    cur.close(); conn.close()

    # Fix timestamps for JSON
    results = []
    for r in rows:
        if r['last_ts']:
            r['last_ts'] = r['last_ts'].isoformat()
            if not r['last_ts'].endswith("Z"): r['last_ts'] += "Z"
        results.append(r)

    return jsonify(results)



@app.route("/history")
def history():
    phone = request.args.get("phone")
    if not phone:
        return jsonify([])

    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT
                sender,
                message,
                media_type,
                media_id,
                status,
                timestamp
            FROM messages
            WHERE user_phone = %s
            ORDER BY timestamp ASC
        """, (phone,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify([
            {
                "sender": r["sender"],
                "message": r["message"],
                "media_type": r["media_type"],
                "media_id": r["media_id"],
                "status": r["status"],
                "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None
            }
            for r in rows
        ])

    except Exception as e:
        print("HISTORY ERROR:", e)
        return jsonify({"error": "history failed"}), 500

@app.route("/send_text", methods=["POST"])
def send_text():
    data = request.json
    phone = data.get("phone")
    text = data.get("text")

    # In a SaaS, you usually determine the 'sender' based on the logged-in user
    # or the active chat context.
    # For now, let's assume we pick the first available account or pass it in data.
    # If you have multiple accounts, you need logic here to pick WHICH one to send from.

    # 🟢 Simple Logic: Get the last account added (or specific one)
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT phone_number_id, access_token FROM whatsapp_accounts LIMIT 1")
    account = cur.fetchone()
    cur.close(); conn.close()

    if not account:
        return jsonify({"error": "No connected WhatsApp accounts found"}), 400

    phone_id = account['phone_number_id']
    token = account['access_token']

    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    try:
        resp = requests.post(url, json=payload, headers=headers)
        json_resp = resp.json()

        # Save to DB
        if "messages" in json_resp:
            wa_id = json_resp["messages"][0]["id"]
            # Save logic...
            return jsonify({"success": True})
        else:
            return jsonify(json_resp), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

@app.route("/wh@app.route("/whatsapp/connect")
@login_required
def whatsapp_connect():
    fb_app_id = os.getenv("FB_APP_ID")
    redirect_uri = url_for("whatsapp_callback", _external=True, _scheme='https')
    state = "security_token_123"
    session["oauth_state"] = state

    # 🟢 UPDATE: Added 'business_management' to the scope
    # This allows us to query 'me/businesses' to find the WABA
    scope = "whatsapp_business_management,whatsapp_business_messaging,business_management"

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

    try:
        # 1. Get the Business Managers this user belongs to
        # We also ask for the WABAs owned by these businesses
        print("DEBUG: Fetching Businesses...")

        # New API Path: Get Businesses -> Then Get Client/Owned WABAs
        businesses_resp = requests.get(
            "https://graph.facebook.com/v20.0/me/businesses",
            headers=headers,
            params={"fields": "id,name,owned_whatsapp_business_accounts,client_whatsapp_business_accounts"}
        ).json()

        print("DEBUG BUSINESS RESP:", businesses_resp)

        if "error" in businesses_resp:
            return f"Meta API Error: {businesses_resp['error']['message']}", 400

        if not businesses_resp.get("data"):
            return "No Business Manager found. Please create a Meta Business Portfolio first at business.facebook.com", 400

        # 2. Find the first WABA available
        waba_id = None
        waba_name = "Unknown"

        for business in businesses_resp["data"]:
            # Check Owned WABAs
            if "owned_whatsapp_business_accounts" in business:
                waba_data = business["owned_whatsapp_business_accounts"]["data"]
                if waba_data:
                    waba_id = waba_data[0]["id"]
                    waba_name = waba_data[0].get("name", "Unknown")
                    break

            # Check Client WABAs (if they are an agency)
            if "client_whatsapp_business_accounts" in business:
                waba_data = business["client_whatsapp_business_accounts"]["data"]
                if waba_data:
                    waba_id = waba_data[0]["id"]
                    waba_name = waba_data[0].get("name", "Unknown")
                    break

        if not waba_id:
            return "Found Business Manager, but no WhatsApp Business Account (WABA) inside it.", 400

        print(f"DEBUG: Found WABA ID: {waba_id} ({waba_name})")

        # 3. Get Phone Number ID from this WABA
        phone_resp = requests.get(
            f"https://graph.facebook.com/v20.0/{waba_id}/phone_numbers",
            headers=headers
        ).json()

        if not phone_resp.get("data"):
            return f"WABA Found ({waba_name}), but no Phone Numbers are registered.", 400

        phone_data = phone_resp["data"][0]
        phone_number_id = phone_data["id"]
        display_phone = phone_data["display_phone_number"]

        # 4. Subscribe App to Webhooks (Critical)
        sub_url = f"https://graph.facebook.com/v20.0/{waba_id}/subscribed_apps"
        requests.post(sub_url, headers=headers)

        # 5. Save to Database
        save_whatsapp_account(waba_id, phone_number_id, display_phone, access_token)

        return redirect("/inbox")

    except Exception as e:
        traceback.print_exc()
        return f"Setup Logic Failed: {str(e)}", 500


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
    cur.execute("""
        SELECT id
        FROM whatsapp_accounts
        WHERE phone_number_id = %s
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
