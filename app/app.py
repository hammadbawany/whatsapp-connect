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



print("BOOT TOKEN:", os.getenv("WA_TOKEN"))
print("BOOT PHONE:", os.getenv("WA_PHONE"))
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-this")
VERIFY_TOKEN = "lifafay123"
WHATSAPP_TOKEN = os.getenv("WA_TOKEN")
PHONE_NUMBER_ID = os.getenv("WA_PHONE")

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

    # =====================================================
    # 1️⃣ WEBHOOK VERIFICATION (META SETUP)
    # =====================================================
    if request.method == "GET":
        VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "lifafay123")

        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200

        return "Verification failed", 403


    # =====================================================
    # 2️⃣ INCOMING EVENTS (POST)
    # =====================================================
    data = request.get_json(silent=True) or {}

    try:
        entry = data.get("entry", [])
        if not entry:
            return "OK", 200

        changes = entry[0].get("changes", [])
        if not changes:
            return "OK", 200

        value = changes[0].get("value", {})

        conn = get_conn()
        cur = conn.cursor()

        # =================================================
        # A) INCOMING MESSAGES (TEXT / IMAGE)
        # =================================================
        messages = value.get("messages", [])

        for msg in messages:
            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                # ---------- TEXT ----------
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")

                    if phone and text:
                        cur.execute("""
                            INSERT INTO messages (
                                user_phone,
                                sender,
                                message,
                                whatsapp_id,
                                status
                            )
                            VALUES (%s, %s, %s, %s, %s)
                        """, (
                            phone,
                            "customer",
                            text,
                            wa_id,
                            "received"
                        ))

                # ---------- IMAGE ----------
                elif msg_type == "image":
                    media_id = msg.get("image", {}).get("id")

                    if phone and media_id:
                        cur.execute("""
                            INSERT INTO messages (
                                user_phone,
                                sender,
                                media_type,
                                media_id,
                                whatsapp_id,
                                status
                            )
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (
                            phone,
                            "customer",
                            "image",
                            media_id,
                            wa_id,
                            "received"
                        ))

                # ---------- OTHER TYPES ----------
                else:
                    print("Ignored message type:", msg_type)

            except Exception:
                print("Message processing error")
                traceback.print_exc()

        # =================================================
        # B) STATUS UPDATES (sent / delivered / read)
        # =================================================
        statuses = value.get("statuses", [])

        for s in statuses:
            try:
                wa_id = s.get("id")
                status = s.get("status")  # sent | delivered | read

                if wa_id and status:
                    cur.execute("""
                        UPDATE messages
                        SET status = %s
                        WHERE whatsapp_id = %s
                    """, (status, wa_id))

            except Exception:
                print("Status update error")
                traceback.print_exc()

        conn.commit()
        cur.close()
        conn.close()

    except Exception:
        print("Webhook fatal error")
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
    try:
        conn = get_conn()
        cur = conn.cursor()  # DictCursor

        cur.execute("""
            SELECT user_phone, MAX(timestamp) AS last_msg
            FROM messages
            GROUP BY user_phone
            ORDER BY last_msg DESC
        """)

        rows = cur.fetchall()

        cur.close()
        conn.close()

        return jsonify([
            {"phone": r["user_phone"]}
            for r in rows
        ])

    except Exception as e:
        print("LIST_USERS ERROR:", e)
        return jsonify({"error": "internal error"}), 500

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

    if not phone or not text:
        return jsonify({"error": "missing data"}), 400

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers)
    resp = r.json()

    # 🔴 IMPORTANT PART — SAVE MESSAGE LOCALLY
    wa_id = None
    try:
        wa_id = resp["messages"][0]["id"]
    except:
        pass

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO messages (
            user_phone,
            sender,
            message,
            whatsapp_id,
            status
        )
        VALUES (%s, %s, %s, %s, %s)
    """, (
        phone,
        "agent",
        text,
        wa_id,
        "sent"
    ))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"success": True})


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
    import traceback
    try:
        print("---- SEND DESIGN START ----")

        phone = request.form.get("phone")
        caption = request.form.get("caption", "")
        file = request.files.get("file")

        print("PHONE:", phone)
        print("CAPTION:", caption)
        print("FILE:", file)

        if not phone or not file:
            print("MISSING DATA")
            return jsonify({"error": "missing data"}), 400

        print("MIMETYPE:", file.mimetype)

        # ⚠️ WhatsApp does NOT support SVG
        if file.mimetype == "image/svg+xml":
            print("SVG UPLOAD BLOCKED")
            return jsonify({"error": "SVG not supported"}), 400

        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False)
        file.save(tmp.name)

        print("TEMP FILE SAVED:", tmp.name)

        upload_url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

        with open(tmp.name, "rb") as f:
            files = {
                "file": (
                    file.filename,
                    f,
                    file.mimetype
                )
            }

            data = {
                "messaging_product": "whatsapp",
                "type": "image"
            }

            upload_resp = requests.post(
                upload_url,
                headers=headers,
                files=files,
                data=data
            )

        print("UPLOAD STATUS:", upload_resp.status_code)
        upload_json = upload_resp.json()
        print("UPLOAD RESPONSE:", upload_json)

        if "id" not in upload_json:
            print("UPLOAD FAILED")
            return jsonify(upload_json), 500

        media_id = upload_json["id"]
        print("MEDIA ID:", media_id)

        msg_url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "image",
            "image": {"id": media_id, "caption": caption}
        }

        msg_resp = requests.post(
            msg_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json"
            }
        )

        print("MESSAGE STATUS:", msg_resp.status_code)
        msg_json = msg_resp.json()
        print("MESSAGE RESPONSE:", msg_json)

        wa_id = msg_json.get("messages", [{}])[0].get("id")

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO messages
            (user_phone, sender, media_type, media_id, message, whatsapp_id, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (
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

        print("---- SEND DESIGN SUCCESS ----")
        return jsonify({"success": True})

    except Exception as e:
        print("❌ SEND DESIGN ERROR")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


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
