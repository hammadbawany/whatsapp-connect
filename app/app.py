# app.py (trimmed/combined - replace your current app.py with this or merge carefully)
import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from db import get_conn  # assumes db.get_conn exists and DATABASE_URL set

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
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

# ---------- Webhook incoming messages & statuses ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})

        # Handle messages
        for msg in value.get("messages", []) or []:
            try:
                phone = msg.get("from")
                text = msg.get("text", {}).get("body")
                wa_id = msg.get("id")

                conn = get_conn()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO messages (user_phone, sender, message, timestamp, whatsapp_id)
                    VALUES (%s, %s, %s, NOW(), %s)
                """, (phone, "customer", text, wa_id))
                conn.commit()
                cur.close()
                conn.close()

            except Exception as e:
                print("DB Insert Error:", e)

        return "OK", 200

    except Exception as e:
        print("Webhook Parse Error:", e)
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
@login_required
def list_users():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_phone FROM messages ORDER BY timestamp DESC")
    users = [{"phone": r["user_phone"]} for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(users)

@app.route("/history")
@login_required
def history():
    phone = request.args.get("phone")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT id, sender, message, media_url, timestamp, status
        FROM messages WHERE user_phone=%s ORDER BY id ASC
    """, (phone,))
    msgs = cur.fetchall()
    cur.close(); conn.close()
    # include typing state
    typing_state = app.config.get("typing_states", {}).get(phone)
    return jsonify({"messages": msgs, "typing": typing_state})

@app.route("/send_text", methods=["POST"])
@login_required
def send_text():
    data = request.json
    phone = data["phone"]
    text = data["text"]
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    r = requests.post(url, json=payload, headers=headers)
    resp = r.json()

    # Save outgoing message and whatsapp_id if returned
    wa_id = None
    if isinstance(resp, dict) and resp.get("messages"):
        wa_id = resp["messages"][0].get("id")

    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (user_phone, sender, message, media_url, timestamp, whatsapp_id, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (phone, "agent", text, None, datetime.utcnow(), wa_id, "sent"))
    conn.commit(); cur.close(); conn.close()

    return jsonify(resp)

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
