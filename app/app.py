# app.py (trimmed/combined - replace your current app.py with this or merge carefully)
from dotenv import load_dotenv
from urllib.parse import quote
import logging
import http.client as http_client

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
from app.db import get_conn
import tempfile
from flask import Response
import traceback
import subprocess
TARGET_WABA_ID = "1628402398537645"
from app.r2_client import get_r2_client
import time
import re
import dropbox
import urllib.parse
from app.plugins.dropbox_plugin import dropbox_bp
from flask import Blueprint, request, redirect, session, jsonify, render_template
from app.plugins.auto_design_sender import design_sender_bp  # <--- ADD THIS
from apscheduler.schedulers.background import BackgroundScheduler
from app.plugins.auto_design_sender import run_scheduled_automation
from app.plugins.voice_bot import voice_bp  # <--- IMPORT
from app.plugins.automations import run_automations

import socket
import atexit
print("[ENV CHECK] R2_ENDPOINT =", os.environ.get("R2_ENDPOINT"))
print("[ENV CHECK] R2_BUCKET   =", os.environ.get("R2_BUCKET"))
print("BOOT TOKEN:", os.getenv("WA_TOKEN"))
print("BOOT PHONE:", os.getenv("WA_PHONE"))
app = Flask(__name__)
app.register_blueprint(dropbox_bp)
app.register_blueprint(design_sender_bp)  # <--- ADD THIS
app.register_blueprint(voice_bp)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-this")
VERIFY_TOKEN = "lifafay123"
WHATSAPP_TOKEN = os.getenv("WA_TOKEN")
APP_BASE_URL = os.getenv("APP_BASE_URL")

PHONE_NUMBER_ID = os.getenv("WA_PHONE")
WABA_ID = os.getenv("WA_WABA_ID")


APP_KEY = "lns4lbjw0ka6sen"
REDIRECT_URI = os.getenv("DROPBOX_REDIRECT_URI")



@app.route("/")
def index():
    # 1. Not Logged In? -> Login
    if "user_id" not in session:
        return redirect(url_for("login"))

    # 2. Logged In? Check for WhatsApp Connection
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM whatsapp_accounts LIMIT 1")
    account = cur.fetchone()
    cur.close()
    conn.close()

    if account:
        # 3. Connected -> SHOW DASHBOARD (New Behavior)
        # Make sure you created 'home.html' in the templates folder!
        return render_template("home.html")
    else:
        # 4. Not Connected -> Go to Onboarding
        return redirect(url_for("connect_page"))

def log(title, payload):
    print("\n" + "=" * 80)
    print(f"{datetime.utcnow().isoformat()} :: {title}")
    print(json.dumps(payload, indent=2))
    print("=" * 80 + "\n")
    sys.stdout.flush()


def auto_log(msg):
    print(f"[AUTOMATION] {msg}", file=sys.stdout)
    sys.stdout.flush()


def get_active_account_id():
    """
    Forces the app to use ONLY the specific WABA ID defined above.
    """
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 FORCE SELECTION OF SPECIFIC WABA
    cur.execute("SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    row = cur.fetchone()

    cur.close(); conn.close()

    if not row:
        print(f"❌ CRITICAL: WABA ID {TARGET_WABA_ID} not found in DB!")
        return None

    return row['id']






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
    # 1️⃣ VERIFICATION
    # -----------------------------------------------------
    if request.method == "GET":
        VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "lifafay123")
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # -----------------------------------------------------
    # 2️⃣ INCOMING EVENTS (POST)
    # -----------------------------------------------------
    try:
        # 🟢 RAW DATA LOGGING (To debug interactive buttons)
        raw_data = request.get_data(as_text=True)
        print(f"\n🔥🔥🔥 RAW WEBHOOK:\n{raw_data}\n", file=sys.stdout)
        sys.stdout.flush()

        data = json.loads(raw_data)

        entry = data.get("entry", [])
        if not entry: return "OK", 200

        changes = entry[0].get("changes", [])
        if not changes: return "OK", 200

        value = changes[0].get("value", {})

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # -------------------------------------------------
        # 🟢 FORCE TARGET WABA ID
        # -------------------------------------------------
        # We ignore incoming metadata match and force the hardcoded target
        # to ensure messages always go to your specific dashboard.

        cur.execute("SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
        account_row = cur.fetchone()

        # Fallback if target not found
        if not account_row:
            cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
            account_row = cur.fetchone()

        whatsapp_account_id = account_row['id'] if account_row else None

        if not whatsapp_account_id:
            print("❌ CRITICAL: No Account ID found in DB! Message will save as NULL.", file=sys.stdout)

        # -------------------------------------------------
        # 3️⃣ CAPTURE CONTACT NAME
        # -------------------------------------------------
        contacts_data = value.get("contacts", [])
        if contacts_data:
            contact = contacts_data[0]
            try:
                cur.execute("""
                    INSERT INTO contacts (phone, name) VALUES (%s, %s)
                    ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
                """, (contact.get("wa_id"), contact.get("profile", {}).get("name")))
            except: pass

        # -------------------------------------------------
        # 4️⃣ PROCESS MESSAGES
        # -------------------------------------------------
        messages = value.get("messages", [])
        for msg in messages:
            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                # Ensure contact exists
                cur.execute("INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING", (phone,))

                # --- TEXT ---
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                    cur.execute("""
                        INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
                        VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                    """, (whatsapp_account_id, phone, text, wa_id))

                # --- MEDIA ---
                elif msg_type in ["image", "video", "audio", "voice", "document", "sticker"]:
                    media_obj = msg.get(msg_type, {})
                    caption = media_obj.get("caption", "")
                    cur.execute("""
                        INSERT INTO messages (whatsapp_account_id, user_phone, sender, media_type, media_id, message, whatsapp_id, status, timestamp)
                        VALUES (%s, %s, 'customer', %s, %s, %s, %s, 'received', NOW())
                    """, (whatsapp_account_id, phone, msg_type, media_obj.get("id"), caption, wa_id))

                # --- INTERACTIVE (Buttons / Lists) ---
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    i_type = interactive.get("type")


                    print(f"🔹 Processing Interactive: {i_type}", file=sys.stdout)

                    resp = ""
                    if i_type == "button_reply":
                        resp = interactive.get("button_reply", {}).get("title")
                    elif i_type == "list_reply":
                        resp = interactive.get("list_reply", {}).get("title")
                    else:
                        resp = f"[{i_type}]"

                    if resp:
                        cur.execute("""
                            INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
                            VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, resp, wa_id))
                elif msg_type == "button":
                    button_payload = msg.get("button", {})
                    text_response = button_payload.get("text")
                    if text_response:
                        cur.execute("""
                            INSERT INTO messages (whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp)
                            VALUES (%s, %s, 'customer', %s, %s, 'received', NOW())
                        """, (whatsapp_account_id, phone, text_response, wa_id))
                # --- UNKNOWN ---
                else:
                    print(f"⚠️ Ignored msg type: {msg_type}", file=sys.stdout)

            except Exception as e:
                print(f"❌ Msg Logic Error: {e}", file=sys.stdout)
                traceback.print_exc()

        # -------------------------------------------------
        # 5️⃣ STATUS UPDATES
        # -------------------------------------------------
        statuses = value.get("statuses", [])
        for s in statuses:
            cur.execute("UPDATE messages SET status = %s WHERE whatsapp_id = %s", (s.get("status"), s.get("id")))

        conn.commit()
        cur.close()
        conn.close()
        sys.stdout.flush()

    except Exception as e:
        print(f"❌ Webhook Fatal Error: {e}", file=sys.stdout)
        traceback.print_exc()
        sys.stdout.flush()

    return "OK", 200
'''

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # -----------------------------------------------------
    # 1️⃣ VERIFICATION
    # -----------------------------------------------------
    if request.method == "GET":
        print("\n🔥🔥🔥 WEBHOOK Get HIT 🔥🔥🔥")
        sys.stdout.flush()

        VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "lifafay123")
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # -----------------------------------------------------
    # 2️⃣ INCOMING EVENTS (POST)
    # -----------------------------------------------------
    try:
        # 🔥 RAW LOGGING (KEEP – useful for debugging replies)
        raw_data = request.get_data(as_text=True)
        print("\n🔥🔥🔥 WEBHOOK Post HIT 🔥🔥🔥")

        print(f"\n🔥🔥🔥 RAW WEBHOOK:\n{raw_data}\n", file=sys.stdout)
        sys.stdout.flush()

        data = json.loads(raw_data)

        entry = data.get("entry", [])
        if not entry:
            return "OK", 200

        changes = entry[0].get("changes", [])
        if not changes:
            return "OK", 200

        value = changes[0].get("value", {})

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # -------------------------------------------------
        # 🟢 FORCE TARGET WABA ID (UNCHANGED)
        # -------------------------------------------------
        cur.execute(
            "SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1",
            (TARGET_WABA_ID,)
        )
        account_row = cur.fetchone()

        if not account_row:
            cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
            account_row = cur.fetchone()

        whatsapp_account_id = account_row["id"] if account_row else None

        if not whatsapp_account_id:
            print("❌ CRITICAL: No Account ID found!", file=sys.stdout)

        # -------------------------------------------------
        # 3️⃣ CAPTURE CONTACT NAME (UNCHANGED)
        # -------------------------------------------------
        contacts_data = value.get("contacts", [])
        if contacts_data:
            contact = contacts_data[0]
            try:
                cur.execute("""
                    INSERT INTO contacts (phone, name)
                    VALUES (%s, %s)
                    ON CONFLICT (phone)
                    DO UPDATE SET name = EXCLUDED.name
                """, (
                    contact.get("wa_id"),
                    contact.get("profile", {}).get("name")
                ))
            except Exception:
                pass

        # -------------------------------------------------
        # 4️⃣ PROCESS MESSAGES (✨ REPLY SUPPORT ADDED)
        # -------------------------------------------------
        messages = value.get("messages", [])
        for msg in messages:
            log("WEBHOOK → FULL MESSAGE OBJECT", msg)

            try:
                phone = msg.get("from")
                wa_id = msg.get("id")
                msg_type = msg.get("type")

                # 🟢 NEW: REPLY CONTEXT (THIS IS THE FIX)
                context_whatsapp_id = None
                context = msg.get("context")
                if context:
                    log("WEBHOOK → CONTEXT FOUND", context)
                    context_whatsapp_id = context.get("id")
                else:
                    print("⚠️ WEBHOOK → NO CONTEXT FOR THIS MESSAGE")
                    sys.stdout.flush()


                # Ensure contact exists
                cur.execute(
                    "INSERT INTO contacts (phone) VALUES (%s) ON CONFLICT (phone) DO NOTHING",
                    (phone,)
                )

                # ---------- TEXT ----------
                if msg_type == "text":
                    text = msg.get("text", {}).get("body")
                    if text:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id,
                                user_phone,
                                sender,
                                message,
                                whatsapp_id,
                                context_whatsapp_id,
                                status,
                                timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, %s, 'received', NOW())
                        """, (
                            whatsapp_account_id,
                            phone,
                            text,
                            wa_id,
                            context_whatsapp_id
                        ))

                        conn.commit()  # 🔥 MUST COMMIT FIRST

                        # 🔥 BACKGROUND AUTOMATION (CORRECT)
                        run_automations(
                            cur=cur,
                            phone=phone,
                            message_text=text,
                            send_text=send_text_internal
                        )

                # ---------- MEDIA ----------
                elif msg_type in ["image", "video", "audio", "voice", "document", "sticker"]:
                    media_obj = msg.get(msg_type, {})
                    caption = media_obj.get("caption", "")
                    media_id = media_obj.get("id")

                    if media_id:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id,
                                user_phone,
                                sender,
                                media_type,
                                media_id,
                                message,
                                whatsapp_id,
                                context_whatsapp_id,
                                status,
                                timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, %s, %s, %s, 'received', NOW())
                        """, (
                            whatsapp_account_id,
                            phone,
                            msg_type,
                            media_id,
                            caption,
                            wa_id,
                            context_whatsapp_id
                        ))

                # ---------- INTERACTIVE ----------
                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    i_type = interactive.get("type")

                    resp = ""
                    if i_type == "button_reply":
                        resp = interactive.get("button_reply", {}).get("title")
                    elif i_type == "list_reply":
                        resp = interactive.get("list_reply", {}).get("title")
                    else:
                        resp = f"[{i_type}]"

                    if resp:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id,
                                user_phone,
                                sender,
                                message,
                                whatsapp_id,
                                context_whatsapp_id,
                                status,
                                timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, %s, 'received', NOW())
                        """, (
                            whatsapp_account_id,
                            phone,
                            resp,
                            wa_id,
                            context_whatsapp_id
                        ))

                # ---------- BUTTON ----------
                elif msg_type == "button":
                    btn = msg.get("button", {})
                    text = btn.get("text")
                    if text:
                        cur.execute("""
                            INSERT INTO messages (
                                whatsapp_account_id,
                                user_phone,
                                sender,
                                message,
                                whatsapp_id,
                                context_whatsapp_id,
                                status,
                                timestamp
                            )
                            VALUES (%s, %s, 'customer', %s, %s, %s, 'received', NOW())
                        """, (
                            whatsapp_account_id,
                            phone,
                            text,
                            wa_id,
                            context_whatsapp_id
                        ))

                else:
                    print(f"⚠️ Ignored msg type: {msg_type}", file=sys.stdout)

            except Exception as e:
                print(f"❌ Message Processing Error: {e}", file=sys.stdout)
                traceback.print_exc()

        # -------------------------------------------------
        # 5️⃣ STATUS UPDATES (UNCHANGED)
        # -------------------------------------------------
        statuses = value.get("statuses", [])
        for s in statuses:
            cur.execute(
                "UPDATE messages SET status = %s WHERE whatsapp_id = %s",
                (s.get("status"), s.get("id"))
            )

        conn.commit()
        cur.close()
        conn.close()
        sys.stdout.flush()

    except Exception as e:
        print(f"❌ Webhook Fatal Error: {e}", file=sys.stdout)
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
#    phone = data.get("phone")
    phone = normalize_phone(data.get("phone"))

    typing = data.get("typing", False)
    # For simplicity we store typing state in-memory dict (per-process)
    # If you run multiple dynos use Redis to share typing state.
    if "typing_states" not in app.config:
        app.config["typing_states"] = {}
    app.config["typing_states"][phone] = {"user": session["user_id"], "typing": typing, "at": datetime.utcnow().isoformat()}
    return jsonify({"ok": True})

#version with tags
@app.route("/list_users")
@login_required
def list_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 1. Get Account ID
    cur.execute("SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()

    if not row:
        cur.close(); conn.close()
        return jsonify([])

    account_id = row['id']

    # 2. Get Users + Last Message
    query = """
        WITH LastMsg AS (
            SELECT
                user_phone,
                MAX(timestamp) as last_ts,
                COUNT(CASE WHEN status = 'received' THEN 1 END) as unread_count
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
    cur.execute(query, (account_id,))
    users = cur.fetchall()

    # 3. Get Tags for these users (Efficient Batch Fetch)
    if users:
        phones = [u['phone'] for u in users]
        cur.execute("""
            SELECT ct.contact_phone, t.name, t.color
            FROM contact_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE ct.contact_phone = ANY(%s)
        """, (phones,))
        tags_rows = cur.fetchall()

        # Map tags to phones
        tags_map = {}
        for row in tags_rows:
            p = row['contact_phone']
            if p not in tags_map: tags_map[p] = []
            tags_map[p].append({'name': row['name'], 'color': row['color']})

        # Attach tags to user objects
        for u in users:
            u['tags'] = tags_map.get(u['phone'], [])
            if u['last_ts']:
                u['last_ts'] = u['last_ts'].isoformat()
                if not u['last_ts'].endswith("Z"): u['last_ts'] += "Z"

    cur.close(); conn.close()
    return jsonify(users)

'''
# ---------- API endpoints (list, history, send) ----------
@app.route("/list_users")
@login_required
def list_users():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 1. Get the Specific Account ID first
    cur.execute("SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    row = cur.fetchone()

    # Fallback
    if not row:
        cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()

    if not row:
        return jsonify([]) # No account connected

    account_id = row['id']

    # 🟢 2. Fetch Users associated with this Account ID
    query = """
        WITH LastMsg AS (
            SELECT
                user_phone,
                MAX(timestamp) as last_ts,
                COUNT(CASE WHEN status = 'received' THEN 1 END) as unread_count
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

    cur.execute(query, (account_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    for r in rows:
        if r['last_ts']:
            r['last_ts'] = r['last_ts'].isoformat()
            if not r['last_ts'].endswith("Z"): r['last_ts'] += "Z"

    return jsonify(rows)
'''
@app.route("/history")
@login_required
def history():
    phone = normalize_phone(request.args.get("phone"))
    account_id = get_active_account_id()

    if not phone or not account_id:
        return jsonify([])

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            m.id,
            m.sender,
            m.message,
            m.media_type,
            m.media_id,
            m.status,
            m.timestamp,
            m.whatsapp_id,
            m.context_whatsapp_id,
             m.deleted_for_me,              -- 🔥 REQUIRED
             m.deleted_for_everyone,        -- 🔥 REQUIRED


            -- reply message fields
            r.sender        AS reply_sender,
            r.message       AS reply_message,
            r.media_type    AS reply_media_type,
            r.media_id      AS reply_media_id,
            r.whatsapp_id   AS reply_whatsapp_id

        FROM messages m
        LEFT JOIN messages r
            ON m.context_whatsapp_id = r.whatsapp_id

        WHERE
            m.user_phone = %s
            AND m.whatsapp_account_id = %s

        ORDER BY m.timestamp ASC
    """, (phone, account_id))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    results = []
    for r in rows:
        ts = r["timestamp"].isoformat() if r["timestamp"] else ""
        if ts and not ts.endswith("Z"):
            ts += "Z"

        item = {
            "id": r["id"],                              # 🔥 REQUIRED
            "sender": r["sender"],
            "message": r["message"],
            "media_type": r["media_type"],
            "media_id": r["media_id"],
            "status": r["status"],
            "timestamp": ts,
            "whatsapp_id": r["whatsapp_id"],
            "deleted_for_me": r["deleted_for_me"],      # 🔥 REQUIRED
            "deleted_for_everyone": r["deleted_for_everyone"]
        }

        if r["reply_whatsapp_id"]:
            item["reply_to"] = {
                "sender": r["reply_sender"],
                "message": r["reply_message"],
                "media_type": r["reply_media_type"],
                "media_id": r["reply_media_id"],
                "whatsapp_id": r["reply_whatsapp_id"]
            }

        results.append(item)

    return jsonify(results)

'''
@app.route("/history")
@login_required
def history():
#    phone = request.args.get("phone")
    phone = normalize_phone(request.args.get("phone"))

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
'''
'''
@app.route("/send_text", methods=["POST"])
def send_text():
    data = request.json
    phone = normalize_phone(data.get("phone"))
    text = data.get("text")
    reply_to = data.get("reply_to")  # may be None

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 FORCE TARGET WABA
    cur.execute(
        "SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1",
        (TARGET_WABA_ID,)
    )
    acc = cur.fetchone()

    if not acc:
        cur.close(); conn.close()
        return jsonify({"error": "No WhatsApp account"}), 400

    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    headers = {
        "Authorization": f"Bearer {acc['access_token']}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    if reply_to:
        payload["context"] = {"message_id": reply_to}

    resp = requests.post(url, headers=headers, json=payload)
    resp_json = resp.json()
    log("SEND_TEXT → PAYLOAD", payload)
    log("SEND_TEXT ← RESPONSE", resp_json)
    messages = resp_json.get("messages")

    wa_id = messages[0].get("id") if isinstance(messages, list) and messages else None

    if not wa_id:
        log("❌ NO WHATSAPP MESSAGE ID RETURNED", resp_json)

    cur.execute("""
        INSERT INTO messages (
            whatsapp_account_id,
            user_phone,
            sender,
            message,
            whatsapp_id,
            context_whatsapp_id,
            status,
            timestamp
        )
        VALUES (%s, %s, 'agent', %s, %s, %s, 'sent', NOW())
    """, (
        acc["id"],
        phone,
        text,
        wa_id,
        reply_to
    ))

    conn.commit()
    cur.close(); conn.close()

    return jsonify({
        "success": True,
        "whatsapp_id": wa_id,
        "is_reply": bool(reply_to)
    })
'''
def send_text_via_meta_and_db(phone, text, reply_to=None):
    # 🔒 Validate reply target
    if reply_to and not reply_to.startswith("wamid."):
        log("❌ INVALID REPLY TARGET (not a wamid)", reply_to)
        reply_to = None

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🟢 FORCE TARGET WABA
    cur.execute(
        "SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1",
        (TARGET_WABA_ID,)
    )
    acc = cur.fetchone()

    if not acc:
        cur.close(); conn.close()
        raise Exception("No WhatsApp account")

    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    headers = {
        "Authorization": f"Bearer {acc['access_token']}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    if reply_to:
        payload["context"] = {"message_id": reply_to}

    log("SEND_TEXT → PAYLOAD", payload)

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp_json = resp.json()

    log("SEND_TEXT ← RESPONSE", resp_json)

    messages = resp_json.get("messages")
    wa_id = messages[0].get("id") if isinstance(messages, list) and messages else None

    cur.execute("""
        INSERT INTO messages (
            whatsapp_account_id,
            user_phone,
            sender,
            message,
            whatsapp_id,
            context_whatsapp_id,
            status,
            timestamp
        )
        VALUES (%s, %s, 'agent', %s, %s, %s, 'sent', NOW())
    """, (
        acc["id"],
        phone,
        text,
        wa_id,
        reply_to
    ))

    conn.commit()
    cur.close(); conn.close()

    return wa_id

@app.route("/send_text", methods=["POST"])
def send_text():
    data = request.json
    phone = normalize_phone(data.get("phone"))
    text = data.get("text")
    reply_to = data.get("reply_to")

    wa_id = send_text_via_meta_and_db(phone, text, reply_to)

    return jsonify({
        "success": True,
        "whatsapp_id": wa_id,
        "is_reply": bool(reply_to)
    })



@app.route("/send_media", methods=["POST"])
@login_required
def send_media():
    data = request.json
#    phone = data["phone"]
    phone = normalize_phone(data["phone"])

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

#if __name__ == "__main__":
#    app.run(debug=True)


# Global variable to hold the socket lock
cron_lock_socket = None

if os.environ.get("ENABLE_CRON") == "true":

    # 1. Check Flask Debug Mode (Local Dev)
    # We only want to run in the reloader process (WERKZEUG_RUN_MAIN)
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":

        try:
            # 2. Try to bind a specific local port (49999)
            # Only ONE process can bind to a port at a time.
            # If this succeeds, this process becomes the "Scheduler Leader".
            cron_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            cron_lock_socket.bind(("127.0.0.1", 49999))

            print(f"🟢 [SCHEDULER] Lock Acquired on Port 49999. Starting Cron on PID {os.getpid()}...")

            scheduler = BackgroundScheduler()
            # Run every 5 minutes
            scheduler.add_job(func=run_scheduled_automation, trigger="interval", minutes=5)
            scheduler.start()

            # Shut down scheduler when app exits
            atexit.register(lambda: scheduler.shutdown())

        except socket.error:
            # If binding fails, another worker is already running the scheduler.
            print(f"🟡 [SCHEDULER] Skipped. Another worker already holds the lock on PID {os.getpid()}.")

if __name__ == "__main__":
    # Local development server
    app.run(debug=True, threaded=True, port=5000)

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
'''
@app.route("/send_design", methods=["POST"])
def send_design():
    try:
#        phone = request.form.get("phone")
        phone = normalize_phone(request.form.get("phone"))

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
'''

@app.route("/send_design", methods=["POST"])
def send_design():
    try:
        phone = normalize_phone(request.form.get("phone"))
        caption = request.form.get("caption", "")
        whatsapp_account_id = request.form.get("whatsapp_account_id")
        file = request.files.get("file")

        if not phone or not file or not whatsapp_account_id:
            return jsonify({"error": "missing data"}), 400

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT phone_number_id, access_token
            FROM whatsapp_accounts
            WHERE id = %s
        """, (whatsapp_account_id,))
        acc = cur.fetchone()

        if not acc:
            cur.close(); conn.close()
            return jsonify({"error": "invalid account"}), 400

        # save temp file
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(delete=False)
        file.save(tmp.name)
        tmp.close()

        upload_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"
        upload_headers = {"Authorization": f"Bearer {acc['access_token']}"}

        with open(tmp.name, "rb") as f:
            upload_resp = requests.post(
                upload_url,
                headers=upload_headers,
                data={"messaging_product": "whatsapp"},
                files={"file": (file.filename, f, file.mimetype)}
            ).json()

        media_id = upload_resp.get("id")
        if not media_id:
            return jsonify(upload_resp), 500

        send_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
        send_resp = requests.post(
            send_url,
            headers={"Authorization": f"Bearer {acc['access_token']}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "image",
                "image": {"id": media_id, "caption": caption}
            }
        ).json()

        wa_id = send_resp.get("messages", [{}])[0].get("id")
        if not wa_id:
            print("❌ send_design: No whatsapp_id", send_resp)

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
            VALUES (%s, %s, 'agent', 'image', %s, %s, %s, 'sent')
        """, (
            whatsapp_account_id,
            phone,
            media_id,
            caption,
            wa_id
        ))

        conn.commit()
        cur.close(); conn.close()
        os.unlink(tmp.name)

        return jsonify({"success": True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "internal error"}), 500

'''
@app.route("/media/<media_id>")
@login_required
def get_media(media_id):
    try:
        # 1. Get the Access Token (Latest one)
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT access_token FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        cur.close(); conn.close()

        if not row:
            print(f"❌ [MEDIA] No access token found in DB to fetch {media_id}", file=sys.stdout)
            return "", 404

        token = row['access_token']
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Ask Meta for the Fresh URL
        meta_url = f"https://graph.facebook.com/v20.0/{media_id}"
        url_resp = requests.get(meta_url, headers=headers).json()

        # 🔍 DEBUG: Print what Meta says
        if "error" in url_resp:
            print(f"❌ [MEDIA] Meta Error for ID {media_id}: {url_resp['error']['message']}", file=sys.stdout)
            sys.stdout.flush()
            return "", 404

        if "url" not in url_resp:
            print(f"❌ [MEDIA] No URL returned for ID {media_id}", file=sys.stdout)
            sys.stdout.flush()
            return "", 404

        # 3. Download the Binary Image
        media_url = url_resp["url"]
        media_data = requests.get(media_url, headers=headers)

        if media_data.status_code != 200:
            print(f"❌ [MEDIA] Failed to download content: {media_data.status_code}", file=sys.stdout)
            return "", 404

        # 4. Serve it to Frontend
        content_type = media_data.headers.get("Content-Type", "image/jpeg")
        return Response(media_data.content, mimetype=content_type)

    except Exception as e:
        print(f"❌ [MEDIA] Critical Server Error: {e}", file=sys.stdout)
        traceback.print_exc()
        sys.stdout.flush()
        return "", 500
'''
'''
def get_r2_key_for_media(media_id):
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT r2_key
            FROM whatsapp_media
            WHERE media_id = %s
            LIMIT 1
        """, (media_id,))

        row = cur.fetchone()
        cur.close()
        conn.close()

        if row and row[0]:
            return row[0]

        return None

    except Exception as e:
        print("[R2][WARN] Failed to fetch r2_key:", e)
        return None
'''
def get_r2_key_for_media(media_id):
    # TEMP: disable R2 lookup safely
    return None

@app.route("/media/<media_id>")
def stream_media(media_id):
    try:
        import requests
        from flask import Response, stream_with_context, request

        # 🔑 Get latest WhatsApp token (SAFE)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token
            FROM whatsapp_accounts
            ORDER BY id DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return "No WhatsApp token", 401

        token = row["access_token"]

        # 1️⃣ Ask Meta for media URL
        meta_resp = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        ).json()

        media_url = meta_resp.get("url")
        if not media_url:
            return "Media not found", 404

        # 2️⃣ Forward Range header (CRITICAL for browser audio)
        headers = {
            "Authorization": f"Bearer {token}"
        }
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]

        # 3️⃣ Stream from Meta
        meta_stream = requests.get(
            media_url,
            headers=headers,
            stream=True,
            timeout=30
        )

        # 4️⃣ Generator
        def generate():
            for chunk in meta_stream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        # 5️⃣ Status code
        status_code = 206 if "Range" in headers else 200

        # 6️⃣ REAL content-type (IMPORTANT)
        content_type = meta_stream.headers.get(
            "Content-Type",
            "audio/ogg"
        )

        # 7️⃣ Build response
        response = Response(
            stream_with_context(generate()),
            status=status_code,
            content_type=content_type
        )

        # 8️⃣ REQUIRED HEADERS FOR BROWSER AUDIO
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Content-Disposition"] = "inline"
        response.headers["Access-Control-Allow-Origin"] = "*"

        if "Content-Length" in meta_stream.headers:
            response.headers["Content-Length"] = meta_stream.headers["Content-Length"]

        if "Content-Range" in meta_stream.headers:
            response.headers["Content-Range"] = meta_stream.headers["Content-Range"]

        return response

    except Exception as e:
        import traceback
        print("[MEDIA][ERROR]", str(e))
        traceback.print_exc()
        return "Media failed to load", 500


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
        phone = normalize_phone(request.form.get("phone"))
        caption = request.form.get("caption", "")
        file = request.files.get("file")
        msg_type = request.form.get("type", "image")

        if not phone or not file:
            return jsonify({"error": "missing data"}), 400

        # 1️⃣ Get WhatsApp account (UNCHANGED LOGIC)
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT id, phone_number_id, access_token
            FROM whatsapp_accounts
            WHERE waba_id = %s
            LIMIT 1
        """, (TARGET_WABA_ID,))
        acc = cur.fetchone()

        if not acc:
            cur.execute("""
                SELECT id, phone_number_id, access_token
                FROM whatsapp_accounts
                ORDER BY id DESC
                LIMIT 1
            """)
            acc = cur.fetchone()

        cur.close()
        conn.close()

        if not acc:
            return jsonify({"error": "No WhatsApp account"}), 400

        # 2️⃣ Read file ONCE
        file_bytes = file.read()
        if len(file_bytes) < 100:
            return jsonify({"error": "File too small"}), 400

        print("📎 Incoming attachment")
        print("Filename:", file.filename)
        print("Type:", msg_type)
        print("Size:", len(file_bytes))

        # 3️⃣ Build upload files (AUDIO vs NON-AUDIO)
        ogg_bytes = None
        audio_kind = None

        if msg_type == "audio":
            # Convert webm → ogg (your existing function)
            ogg_bytes, duration = convert_webm_to_ogg(file_bytes)
            audio_kind = detect_voice_or_audio(duration)

            files = {
                "file": ("voice.ogg", ogg_bytes, "audio/ogg; codecs=opus")
            }
            caption = ""  # WhatsApp does not allow captions on voice
        else:
            # 🔴 THIS WAS MISSING → restores image/video/document support
            files = {
                "file": (file.filename, file_bytes, file.mimetype)
            }

        # 4️⃣ Upload media to Meta (COMMON PATH)
        upload_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"
        headers = {"Authorization": f"Bearer {acc['access_token']}"}

        up_resp = requests.post(
            upload_url,
            headers=headers,
            files=files,
            data={"messaging_product": "whatsapp"}
        ).json()

        print("📤 Meta upload response:", up_resp)

        media_id = up_resp.get("id")
        if not media_id:
            return jsonify({"error": "Media upload failed", "meta": up_resp}), 500

        # 5️⃣ Small delay for audio indexing ONLY
        if msg_type == "audio":
            time.sleep(1.0)

        # 6️⃣ Build send payload (FULL FEATURE SET)
        send_payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": msg_type,
        }

        if msg_type == "audio":
            # ✅ iOS-safe behavior: send as normal audio (no voice flag)
            # This preserves reliability across all platforms
            send_payload["audio"] = {
                "id": media_id
            }
        else:
            send_payload[msg_type] = {"id": media_id}

        # Caption logic (same as old code)
        if caption and msg_type in ["image", "video", "document"]:
            send_payload[msg_type]["caption"] = caption

        print("🚀 FINAL SEND PAYLOAD:", send_payload)

        # 7️⃣ Send message (ONCE ONLY)
        send_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
        send_resp = requests.post(
            send_url,
            headers={
                "Authorization": f"Bearer {acc['access_token']}",
                "Content-Type": "application/json"
            },
            json=send_payload
        ).json()

        print("📨 Meta send response:", send_resp)

        wa_id = send_resp.get("messages", [{}])[0].get("id")
        if not wa_id:
            print("[WARN] WhatsApp message id missing", send_resp)

        # 8️⃣ Save message to DB (UNCHANGED SCHEMA)
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO messages (
                whatsapp_account_id,
                user_phone,
                sender,
                media_type,
                media_id,
                message,
                whatsapp_id,
                status,
                timestamp
            )
            VALUES (%s, %s, 'agent', %s, %s, %s, %s, 'sent', NOW())
        """, (
            acc["id"],
            phone,
            msg_type,
            media_id,
            caption,
            wa_id
        ))

        conn.commit()
        cur.close()
        conn.close()

        # 9️⃣ 🔵 Non-blocking R2 upload (AUDIO ONLY)
        if msg_type == "audio" and ogg_bytes:
            try:
                print(f"[R2] Upload via Worker media_id={media_id}")
                r2_key = upload_audio_via_worker(media_id, ogg_bytes)
                print(f"[R2] Worker upload OK key={r2_key}")
            except Exception as e:
                print("[R2][NON-FATAL] Worker upload failed")
                print("[R2][ERROR]", e)

        return jsonify({"success": True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


'''

@app.route("/send_attachment", methods=["POST"])
def send_attachment():
    try:
        phone = normalize_phone(request.form.get("phone"))
        caption = request.form.get("caption", "")
        file = request.files.get("file")
        msg_type = request.form.get("type", "image")

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1",
            (TARGET_WABA_ID,)
        )
        acc = cur.fetchone()
        cur.close(); conn.close()

        if not acc:
            return jsonify({"error": "No account"}), 400

        upload_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/media"
        upload_headers = {"Authorization": f"Bearer {acc['access_token']}"}

        files = {"file": (file.filename, file.read(), file.mimetype)}
        upload_resp = requests.post(
            upload_url,
            headers=upload_headers,
            files=files,
            data={"messaging_product": "whatsapp"}
        ).json()

        media_id = upload_resp.get("id")
        if not media_id:
            return jsonify(upload_resp), 500

        send_url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": msg_type,
            msg_type: {"id": media_id}
        }
        if caption and msg_type in ["image", "video", "document"]:
            payload[msg_type]["caption"] = caption

        send_resp = requests.post(
            send_url,
            headers={"Authorization": f"Bearer {acc['access_token']}", "Content-Type": "application/json"},
            json=payload
        ).json()

        wa_id = send_resp.get("messages", [{}])[0].get("id")
        if not wa_id:
            print("❌ send_attachment: No whatsapp_id", send_resp)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO messages (
                whatsapp_account_id,
                user_phone,
                sender,
                media_type,
                media_id,
                message,
                whatsapp_id,
                status,
                timestamp
            )
            VALUES (%s, %s, 'agent', %s, %s, %s, %s, 'sent', NOW())
        """, (
            acc["id"],
            phone,
            msg_type,
            media_id,
            caption,
            wa_id
        ))
        conn.commit()
        cur.close(); conn.close()

        return jsonify({"success": True})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
'''

        # 3. Add Tag (For Filtering)
@app.route("/set_tag", methods=["POST"])
def set_tag():
    data = request.json
    #phone = data.get("phone")
    phone = normalize_phone(data.get("phone"))

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
    #phone = data.get("phone")
    phone = normalize_phone(data.get("phone"))

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
        #phone = data.get("phone")
        phone = normalize_phone(data.get("phone"))

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

'''
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
'''

@app.route("/get_templates")
@login_required
def get_templates():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    # 🟢 HARDCODED
    cur.execute("SELECT waba_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    acc = cur.fetchone(); cur.close(); conn.close()
    if not acc: return jsonify({"error": "No account"}), 400
    return jsonify(requests.get(f"https://graph.facebook.com/v20.0/{acc['waba_id']}/message_templates?status=APPROVED&limit=100", headers={"Authorization": f"Bearer {acc['access_token']}"}).json())

'''
#new code from gemini
@app.route("/get_templates")
@login_required
def get_templates():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    # Use Target WABA Logic
    cur.execute("SELECT waba_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    acc = cur.fetchone()
    # Fallback
    if not acc:
        cur.execute("SELECT waba_id, access_token FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        acc = cur.fetchone()

    if not acc: return jsonify({"error": "No account"}), 400

    # Fetch templates
    url = f"https://graph.facebook.com/v20.0/{acc['waba_id']}/message_templates?limit=100"
    resp = requests.get(url, headers={"Authorization": f"Bearer {acc['access_token']}"}).json()

    return jsonify(resp)
'''
# ==========================================
# 🟢 NEW: SEND TEMPLATE MESSAGE
# ==========================================

'''
@app.route("/send_template", methods=["POST"])
def send_template():
    try:
        data = request.json
        phone = data.get("phone")
        template_name = data.get("template_name")
        language = data.get("language", "en_US")
        # Use the full text passed from frontend for DB saving
        template_body = data.get("template_body", f"[Template: {template_name}]")

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1. FORCE TARGET WABA ID
        cur.execute("SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
        acc = cur.fetchone()

        if not acc:
            cur.close(); conn.close()
            return jsonify({"error": "No account connected"}), 400

        # 2. Prepare Base Payload
        url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
        headers = {
            "Authorization": f"Bearer {acc['access_token']}",
            "Content-Type": "application/json"
        }

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language}
            }
        }

        # 3. Attempt Send
        print(f"📤 Sending Template: {template_name}")
        resp = requests.post(url, headers=headers, json=payload)
        json_resp = resp.json()

        # 🟢 4. AUTO-FIX FOR MISSING PARAMETERS (Error 132000)
        # If Meta says "Missing Params", we retry with a default "Customer" value
        if "error" in json_resp and json_resp["error"].get("code") == 132000:
            print(f"⚠️ Template requires variables. Retrying with default 'Valued Customer'...")

            # Add a default body parameter
            payload["template"]["components"] = [{
                "type": "body",
                "parameters": [
                    {
                        "type": "text",
                        "text": "Valued Customer" # <--- The default value for {{1}}
                    }
                ]
            }]

            # Retry Send
            resp = requests.post(url, headers=headers, json=payload)
            json_resp = resp.json()

        # 5. Handle Final Result
        if "messages" not in json_resp:
            print("❌ Meta Template Error:", json_resp)
            return jsonify(json_resp), 400

        wa_id = json_resp["messages"][0]["id"]

        # 6. Save to DB
        cur.execute("""
            INSERT INTO messages (
                whatsapp_account_id, user_phone, sender, message, whatsapp_id, status, timestamp
            )
            VALUES (%s, %s, 'agent', %s, %s, 'sent', NOW())
        """, (acc['id'], phone, template_body, wa_id))

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"success": True})

    except Exception as e:
        print("❌ Send Template Exception:", e)
        return jsonify({"error": str(e)}), 500
'''

@app.route("/send_template", methods=["POST"])
def send_template():
    data = request.json
    phone = normalize_phone(data.get("phone"))
    template_name = data.get("template_name")
    language = data.get("language", "en_US")
    template_body = data.get("template_body", f"[Template: {template_name}]")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        "SELECT id, phone_number_id, access_token FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1",
        (TARGET_WABA_ID,)
    )
    acc = cur.fetchone()

    if not acc:
        cur.close(); conn.close()
        return jsonify({"error": "No account"}), 400

    url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
    headers = {
        "Authorization": f"Bearer {acc['access_token']}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language}
        }
    }

    resp = requests.post(url, headers=headers, json=payload).json()
    wa_id = resp.get("messages", [{}])[0].get("id")

    if not wa_id:
        print("❌ send_template: No whatsapp_id", resp)

    cur.execute("""
        INSERT INTO messages (
            whatsapp_account_id,
            user_phone,
            sender,
            message,
            whatsapp_id,
            status,
            timestamp
        )
        VALUES (%s, %s, 'agent', %s, %s, 'sent', NOW())
    """, (
        acc["id"],
        phone,
        template_body,
        wa_id
    ))

    conn.commit()
    cur.close(); conn.close()

    return jsonify({"success": True})

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



'''
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
'''

@app.route("/connect")
@login_required
def connect_page():
    review_mode = request.args.get("review") == "1"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM whatsapp_accounts LIMIT 1")
    already_connected = cur.fetchone() is not None
    cur.close()
    conn.close()

    # Normal behavior (production)
    if already_connected and not review_mode:
        return redirect(url_for("inbox"))

    # Review mode OR not connected
    return render_template(
        "connect.html",
        already_connected=already_connected,
        review_mode=review_mode
    )
'''
# --- HELPER: NORMALIZE PHONE ---
def normalize_phone(phone):
    """
    Converts 03001234567 -> 923001234567
    Removes + and spaces.
    """
    if not phone: return ""
    p = str(phone).strip().replace("+", "").replace(" ", "").replace("-", "")

    # Specific logic for Pakistan (03 -> 923)
    if p.startswith("03") and len(p) == 11:
        return "92" + p[1:]

    return p
'''


def normalize_phone(phone):
    """
    Normalizes phone numbers into digit-only international format.
    Examples:
    03001234567      -> 923001234567
    +923001234567   -> 923001234567
    +14107263057    -> 14107263057
    1410-726-3057   -> 14107263057
    """
    if not phone:
        return ""

    # keep digits only
    p = re.sub(r"\D", "", str(phone))

    # Pakistan local → international
    if p.startswith("03") and len(p) == 11:
        return "92" + p[1:]

    return p

@app.route("/mark_unread", methods=["POST"])
@login_required
def mark_unread():
    phone = request.json.get("phone")
    account_id = get_active_account_id()

    if not phone or not account_id:
        return jsonify({"error": "Missing data"}), 400

    try:
        conn = get_conn()
        cur = conn.cursor()

        # Find the ID of the VERY LAST message in this conversation
        # and set its status back to 'received'.
        # This will make the unread counter reappear.
        cur.execute("""
            UPDATE messages
            SET status = 'received'
            WHERE id = (
                SELECT id FROM messages
                WHERE user_phone = %s AND whatsapp_account_id = %s
                ORDER BY timestamp DESC
                LIMIT 1
            )
        """, (phone, account_id))

        conn.commit()
        cur.close(); conn.close()

        return jsonify({"success": True})
    except Exception as e:
        print("Mark Unread Error:", e)
        return jsonify({"error": "Failed"}), 500

@app.route("/unread_counts")
@login_required
def unread_counts():
    # Reuse the logic to get the correct account ID
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM whatsapp_accounts WHERE waba_id = %s LIMIT 1", (TARGET_WABA_ID,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id FROM whatsapp_accounts ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()

    if not row:
        cur.close(); conn.close()
        return jsonify({})

    account_id = row['id']

    cur.execute("""
        SELECT user_phone, COUNT(*) as unread
        FROM messages
        WHERE whatsapp_account_id = %s AND status = 'received'
        GROUP BY user_phone
    """, (account_id,))

    rows = cur.fetchall()
    cur.close(); conn.close()

    counts = {r['user_phone']: r['unread'] for r in rows}
    return jsonify(counts)


def convert_webm_to_ogg(webm_bytes):
    import subprocess, tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as inp:
        inp.write(webm_bytes)
        inp_path = inp.name

    out_path = inp_path.replace(".webm", ".ogg")

    subprocess.run([
        "ffmpeg",
        "-y",
        "-i", inp_path,
        "-vn",
        "-map_metadata", "-1",
        "-ac", "1",
        "-ar", "48000",              # 🔥 MUST BE 48000 FOR iOS
        "-c:a", "libopus",
        "-b:a", "24k",               # 🔥 NOT 16k
        "-application", "voip",
        "-frame_duration", "20",     # 🔥 REQUIRED FOR iOS
        "-packet_loss", "5",
        out_path
    ], check=True)

    with open(out_path, "rb") as f:
        ogg_bytes = f.read()

    os.unlink(inp_path)
    os.unlink(out_path)

    return ogg_bytes

@app.route("/admin/upload_media/<media_id>")
def upload_media_to_r2(media_id):
    print("🔥UPLOAD ROUTE FILE LOADED🔥🔥🔥 ")

    # 1️⃣ Get WhatsApp token from DB (reuse your existing logic)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT access_token
        FROM whatsapp_accounts
        ORDER BY id DESC
        LIMIT 1
    """)
    acc = cur.fetchone()
    cur.close()
    conn.close()

    if not acc:
        return "No WhatsApp token", 500

    token = acc["access_token"]

    # 2️⃣ Get media URL from Meta
    meta = requests.get(
        f"https://graph.facebook.com/v20.0/{media_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15
    ).json()

    media_url = meta.get("url")
    if not media_url:
        return "Media URL not found", 404

    # 3️⃣ Download media from Meta
    media_resp = requests.get(
        media_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30
    )

    # 4️⃣ Upload to R2
    r2 = get_r2_client()
    key = f"media/voice/{media_id}.ogg"

    r2.put_object(
        Bucket=os.environ["R2_BUCKET"],
        Key=key,
        Body=media_resp.content,
        ContentType="audio/ogg"
    )

    return {
        "status": "uploaded",
        "bucket": os.environ["R2_BUCKET"],
        "key": key
    }

def upload_audio_to_r2(media_id, token):
    import sys
    import os
    import requests
    import boto3
    import ssl
    import mimetypes
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager
    from botocore.config import Config

    def log(msg):
        print(f"🔥 [R2] {msg}", file=sys.stdout, flush=True)

    # 🛠️ SSL Fix for Heroku
    class LegacySSLAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False):
            ctx = ssl.create_default_context()
            try:
                ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            except Exception:
                pass
            self.poolmanager = PoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                ssl_context=ctx
            )

    log(f"Processing Media ID: {media_id}")

    try:
        # 1️⃣ Get URL
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        ).json()

        media_url = meta.get("url")
        # Fallback to 'mime_type' from Meta metadata if available
        meta_mime = meta.get("mime_type")

        if not media_url:
            raise Exception("No URL found in Meta response")

        # 2️⃣ Download
        audio_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )

        # 🕵️ DETECT REAL CONTENT TYPE
        content_type = audio_resp.headers.get("Content-Type")

        # If headers are generic, use the metadata from Step 1
        if not content_type or content_type == 'application/octet-stream':
            content_type = meta_mime or "audio/ogg"

        # Check for non-audio junk
        if "json" in content_type or "text" in content_type or "html" in content_type:
            log(f"❌ Error: Meta returned {content_type} instead of audio.")
            log(f"Body: {audio_resp.text}")
            raise Exception("Invalid file content (not audio)")

        audio_bytes = audio_resp.content
        if len(audio_bytes) < 100:
            raise Exception(f"File too small ({len(audio_bytes)} bytes). Likely an error.")

        # 3️⃣ Determine Extension
        # Default to .ogg, but change if it's mp4/aac/mp3
        ext = ".ogg"
        if "mp4" in content_type:
            ext = ".mp4"
        elif "mpeg" in content_type or "mp3" in content_type:
            ext = ".mp3"
        elif "aac" in content_type:
            ext = ".aac"
        elif "amr" in content_type:
            ext = ".amr"

        log(f"✅ Detected Type: {content_type} -> Extension: {ext}")

        # 4️⃣ Generate URL (Offline)
        endpoint = os.environ.get('R2_ENDPOINT')
        if endpoint and endpoint.endswith('/'): endpoint = endpoint[:-1]

        s3_signer = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
            config=Config(signature_version='s3v4'),
            region_name='auto'
        )

        # Use the dynamic extension
        key = f"media/audio/{media_id}{ext}"

        upload_url = s3_signer.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': os.environ.get("R2_BUCKET"),
                'Key': key,
                'ContentType': content_type # 🚨 CRITICAL: Tell R2 the real type
            },
            ExpiresIn=300
        )

        # 5️⃣ Upload
        session = requests.Session()
        session.mount('https://', LegacySSLAdapter())

        r2_resp = session.put(
            upload_url,
            data=audio_bytes,
            headers={'Content-Type': content_type}, # 🚨 Match header
            timeout=30
        )

        if r2_resp.status_code not in [200, 201, 204]:
            raise Exception(f"Upload failed: {r2_resp.status_code}")

        log(f"✅✅✅ Uploaded successfully: {key}")

        # Return the key so your database saves the correct extension
        return key

    except Exception as e:
        log(f"❌ FATAL: {e}")
        raise e

        '''
def upload_audio_to_r2(media_id, token):
    import requests
    import os
    from r2_client import generate_presigned_put

    # 1️⃣ Get media URL from Meta
    meta = requests.get(
        f"https://graph.facebook.com/v20.0/{media_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10
    ).json()

    media_url = meta.get("url")
    if not media_url:
        raise Exception("Meta media URL not found")

    # 2️⃣ Download audio from Meta
    audio_resp = requests.get(
        media_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20
    )

    audio_bytes = audio_resp.content
    if not audio_bytes:
        raise Exception("Downloaded audio is empty")

    # 3️⃣ Prepare R2 key
    key = f"media/audio/{media_id}.ogg"

    # 4️⃣ Generate presigned PUT URL
    put_url = generate_presigned_put(
        key=key,
        content_type="audio/ogg"
    )

    # 5️⃣ Upload via HTTPS PUT (NO boto3 here)
    put_resp = requests.put(
        put_url,
        data=audio_bytes,
        headers={"Content-Type": "audio/ogg"},
        timeout=20
    )

    if put_resp.status_code not in (200, 204):
        raise Exception(
            f"R2 PUT failed {put_resp.status_code}: {put_resp.text}"
        )

    print(f"[R2][SUCCESS] Uploaded via presigned PUT key={key}")
    return key

'''
def get_latest_whatsapp_token():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT access_token
        FROM whatsapp_accounts
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not row.get("access_token"):
        raise Exception("No WhatsApp access token found")

    return row["access_token"]


def upload_audio_via_worker(media_id, ogg_bytes):
    url = f"{os.environ['WORKER_UPLOAD_BASE']}/media/audio/{media_id}.ogg"

    headers = {
        "Content-Type": "audio/ogg",
        "x-upload-secret": os.environ["WORKER_UPLOAD_SECRET"],
    }

    resp = requests.put(
        url,
        data=ogg_bytes,
        headers=headers,
        timeout=15
    )

    if resp.status_code != 200:
        raise Exception(f"Worker upload failed: {resp.text}")

    return f"media/audio/{media_id}.ogg"



@app.route('/delete_for_everyone', methods=['POST'])
def delete_for_everyone():
    try:
        # 🟢 1. SETUP DEEP LOGGING
        # This will print the RAW HTTP request/response to your terminal
        http_client.HTTPConnection.debuglevel = 1
        logging.basicConfig()
        logging.getLogger().setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True

        print("\n" + "="*50)
        print("🛑 START DELETE FOR EVERYONE DEBUG")
        print("="*50)

        data = request.get_json(silent=True) or {}
        msg_id = data.get("id")

        print(f"🔹 Incoming Payload ID: {msg_id}")

        if not msg_id:
            return jsonify({"error": "missing id"}), 400

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 2️⃣ Load message details
        cur.execute("""
            SELECT
                m.id,
                m.sender,
                m.whatsapp_id,
                a.phone_number_id,
                a.access_token,
                a.waba_id
            FROM messages m
            JOIN whatsapp_accounts a
              ON a.id = m.whatsapp_account_id
            WHERE m.id = %s
        """, (msg_id,))
        msg = cur.fetchone()

        if not msg:
            print("❌ Error: Message not found in DB")
            cur.close(); conn.close()
            return jsonify({"error": "message_not_found"}), 404

        print(f"🔹 DB Data Found:")
        print(f"   - Sender: {msg['sender']}")
        print(f"   - WAMID: {msg['whatsapp_id']}")
        print(f"   - Phone ID: {msg['phone_number_id']}")

        # 3️⃣ Validation
        if msg["sender"] != "agent":
            print("❌ Error: Sender is not agent")
            cur.close(); conn.close()
            return jsonify({"error": "cannot_delete_customer_message"}), 403

        if not msg["whatsapp_id"]:
            print("❌ Error: No Whatsapp ID (WAMID)")
            cur.close(); conn.close()
            return jsonify({"error": "no_wamid_found"}), 400

        # 4️⃣ Prepare Request
        target_url = f"https://graph.facebook.com/v20.0/{msg['phone_number_id']}/messages"

        # 🔒 STRICT JSON PAYLOAD
        payload = json.dumps({
            "message_id": msg["whatsapp_id"]
        })

        headers = {
            "Authorization": f"Bearer {msg['access_token']}",
            "Content-Type": "application/json"
        }

        print(f"🔹 Sending DELETE Request:")
        print(f"   - URL: {target_url}")
        print(f"   - Body: {payload}")

        # ⚡ EXECUTE REQUEST
        # We use requests.request to ensure 'data' is attached to DELETE
        resp = requests.request("DELETE", target_url, headers=headers, data=payload)

        # 🛑 TURN OFF DEEP LOGGING
        http_client.HTTPConnection.debuglevel = 0

        try:
            resp_json = resp.json()
        except:
            resp_json = {"text": resp.text}

        print(f"\n🔹 Meta Response:")
        print(f"   - Status: {resp.status_code}")
        print(f"   - Body: {resp_json}")
        print("="*50 + "\n")

        # 5️⃣ Success Handler
        if resp.status_code == 200 and resp_json.get("success"):
            print("✅ Meta confirmed success. Updating DB...")
            cur.execute("""
                UPDATE messages
                SET deleted_for_everyone = TRUE
                WHERE id = %s
            """, (msg_id,))
            conn.commit()
            cur.close(); conn.close()
            return jsonify({"success": True, "meta_deleted": True})

        # 6️⃣ Failure Handler
        cur.close(); conn.close()
        return jsonify({
            "success": False,
            "error": resp_json.get("error", {}).get("message", "Meta API Error"),
            "details": resp_json
        }), 400

    except Exception as e:
        print(f"❌ EXCEPTION: {e}")
        traceback.print_exc()
        return jsonify({"error": "internal_error"}), 500


@app.route('/delete_for_me', methods=['POST'])
def delete_for_me():
    try:
        data = request.get_json(silent=True) or {}
        msg_id = data.get('id')
        print(f"\n🟢 [DELETE_FOR_ME] Requested ID: {msg_id}")

        if not msg_id:
            return jsonify({"error": "missing id"}), 400

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            UPDATE messages
            SET deleted_for_me = TRUE
            WHERE id = %s
        """, (msg_id,))

        rows = cur.rowcount

        if rows > 0:
            conn.commit()
            print(f"✅ [DELETE_FOR_ME] Success. Rows updated: {rows}")
            cur.close(); conn.close()
            return jsonify({"success": True})
        else:
            conn.rollback()
            print(f"⚠️ [DELETE_FOR_ME] Failed. ID not found in DB.")
            cur.close(); conn.close()
            return jsonify({"error": "message_not_found"}), 404

    except Exception as e:
        print(f"❌ [DELETE_FOR_ME] Error: {e}")
        traceback.print_exc()
        return jsonify({"error": "internal_error"}), 500

def wait_for_media_ready(media_id, token, timeout=5):
    import time, requests

    url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}

    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(url, headers=headers)
        if r.status_code == 200 and r.json().get("url"):
            return True
        time.sleep(0.4)

    return False

def detect_voice_or_audio(duration_seconds):
    """
    WhatsApp iOS safe detection
    """
    if duration_seconds <= 10:
        return "voice"
    return "audio"


# ==========================================
# 🟢 API: SEND ORDER CONFIRMATION (3 VARIABLES)
# ==========================================
@app.route("/api/external/send_order", methods=["POST"])
def external_send_order():
    try:

            # =====================================================
        # 1️⃣ SECURITY CHECK
        # =====================================================
        incoming_key = request.headers.get("X-API-Key")
        expected_key = os.getenv("API_SECRET", "default_secret")
        print("🔑 Incoming Key:", incoming_key)
        print("🔑 Expected Key:", expected_key)
        if incoming_key != expected_key:
            return jsonify({"error": "Unauthorized"}), 401

        # =====================================================
        # 2️⃣ INPUT DATA
        # =====================================================
        data = request.get_json(silent=True) or {}

        raw_phone = data.get("phone")
        name = str(data.get("name", "")).strip()
        order_number = str(data.get("order_number", "")).strip()
        delivery_date = str(data.get("delivery_date", "")).strip()
        amount = str(data.get("amount", "")).strip()

        template_name = data.get("template_name", "order_received")
        language = data.get("language", "en_US")

        phone = normalize_phone(raw_phone)
        if not phone:
            return jsonify({"error": "Invalid phone number"}), 400

        if not all([name, order_number, delivery_date, amount]):
            return jsonify({"error": "Missing template variables"}), 400

        # =====================================================
        # 3️⃣ LOAD WHATSAPP ACCOUNT
        # =====================================================
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
        INSERT INTO contact_tags (contact_phone, tag_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """, (phone, 1))

        cur.execute("""
            SELECT id, phone_number_id, access_token
            FROM whatsapp_accounts
            WHERE waba_id = %s
            LIMIT 1
        """, (TARGET_WABA_ID,))
        acc = cur.fetchone()

        if not acc:
            cur.close()
            conn.close()
            return jsonify({"error": "WhatsApp account not connected"}), 500

        # =====================================================
        # 4️⃣ META PAYLOAD (POSITIONAL PARAMETERS)
        # =====================================================
        url = f"https://graph.facebook.com/v20.0/{acc['phone_number_id']}/messages"
        headers = {
            "Authorization": f"Bearer {acc['access_token']}",
            "Content-Type": "application/json"
        }

        payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,          # order_management_3
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": name},           # {{1}}
                        {"type": "text", "text": order_number},   # {{2}}
                        {"type": "text", "text": delivery_date},  # {{3}}
                        {"type": "text", "text": amount}          # {{4}}
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [
                        {"type": "payload", "payload": "CONFIRM_ORDER"}
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "1",
                    "parameters": [
                        {"type": "payload", "payload": "CANCEL_ORDER"}
                    ]
                }
            ]
        }
    }


        print("\n🚀 SENDING TO META:")
        print(json.dumps(payload, indent=2))

        # =====================================================
        # 5️⃣ SEND TO META
        # =====================================================
        resp = requests.post(url, headers=headers, json=payload, timeout=10)

        print("📡 META STATUS:", resp.status_code)
        print("📡 META RAW:", resp.text)

        try:
            resp_json = resp.json()
        except Exception:
            return jsonify({
                "success": False,
                "error": "Meta returned non-JSON",
                "raw": resp.text
            }), 500


        if resp.status_code not in [200, 201]:
            print("❌ META ERROR:", resp_json)
            return jsonify({"success": False, "meta_error": resp_json}), 400

        wa_id = resp_json["messages"][0]["id"]

        # =====================================================
        # 6️⃣ SAVE MESSAGE TO DB (MATCH TEMPLATE)
        # =====================================================
        full_message_body = (
            f"Hi {name}, we've received your order.\n\n"
            f"Your order number is {order_number}.\n\n"
            f"Estimated delivery: {delivery_date}.\n\n"
            f"Payable Amount: rs {amount}.\n\n"
            "Click below to manage your order."
        )

        cur.execute("""
            INSERT INTO messages (
                whatsapp_account_id,
                user_phone,
                sender,
                message,
                whatsapp_id,
                status,
                timestamp
            )
            VALUES (%s, %s, 'agent', %s, %s, 'sent', NOW())
        """, (acc["id"], phone, full_message_body, wa_id))

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"success": True, "wa_id": wa_id})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


###########
###templater syncing


@app.route("/sync_templates", methods=["POST"])
def sync_templates_basic():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1️⃣ Get WhatsApp account (TARGET_WABA_ID only)
        cur.execute("""
            SELECT waba_id, access_token
            FROM whatsapp_accounts
            WHERE waba_id = %s
            LIMIT 1
        """, (TARGET_WABA_ID,))
        acc = cur.fetchone()

        if not acc:
            cur.close()
            conn.close()
            return jsonify({"error": "No WhatsApp account"}), 400

        # 2️⃣ Fetch templates from Meta
        url = f"https://graph.facebook.com/v20.0/{acc['waba_id']}/message_templates"
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {acc['access_token']}"}
        ).json()

        templates = resp.get("data", [])
        synced = 0

        # 3️⃣ Insert / update aliases
        for tpl in templates:
            meta_name = tpl.get("name")
            language = tpl.get("language")

            # Extract BODY text for UI preview
            preview_text = ""
            for c in tpl.get("components", []):
                if c.get("type") == "BODY":
                    preview_text = c.get("text", "")
                    break

            cur.execute("""
                INSERT INTO template_aliases (
                    meta_template_name,
                    internal_name,
                    visible_in_ui,
                    usage_type,
                    language_code,
                    preview_text
                )
                VALUES (%s, %s, TRUE, 'manual', %s, %s)
                ON CONFLICT (meta_template_name, language_code)
                DO UPDATE SET
                    preview_text = EXCLUDED.preview_text
            """, (
                meta_name,
                meta_name.replace("_", " ").title(),
                language,
                preview_text
            ))

            synced += 1

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "success": True,
            "templates_synced": synced
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/template_aliases", methods=["GET"])
def get_template_aliases():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            id,
            meta_template_name,
            internal_name,
            visible_in_ui,
            usage_type,
            language_code,
            preview_text
        FROM template_aliases
        ORDER BY created_at DESC
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(rows)


@app.route("/template_aliases/<int:tpl_id>", methods=["POST"])
def update_template_alias(tpl_id):
    data = request.get_json()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE template_aliases
        SET
            internal_name = %s,
            visible_in_ui = %s,
            usage_type = %s
        WHERE id = %s
    """, (
        data["internal_name"],
        data["visible_in_ui"],
        data["usage_type"],
        tpl_id
    ))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"success": True})


@app.route("/admin/sync_templates", methods=["POST"])
def sync_templates():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Get latest WhatsApp account
    cur.execute("""
        SELECT waba_id, access_token
        FROM whatsapp_accounts
        ORDER BY id DESC
        LIMIT 1
    """)
    acc = cur.fetchone()

    if not acc:
        return jsonify({"error": "No WhatsApp account"}), 400

    url = f"https://graph.facebook.com/v20.0/{acc['waba_id']}/message_templates"
    headers = {"Authorization": f"Bearer {acc['access_token']}"}

    resp = requests.get(url, headers=headers).json()

    if "data" not in resp:
        return jsonify(resp), 400

    synced = 0

    for tpl in resp["data"]:
        for lang in tpl.get("language", []):

            cur.execute("""
                INSERT INTO template_aliases (
                    meta_template_name,
                    language_code,
                    internal_name,
                    usage_type
                )
                VALUES (%s, %s, %s, 'manual')
                ON CONFLICT (meta_template_name, language_code)
                DO NOTHING
            """, (
                tpl["name"],
                lang,
                tpl["name"].replace("_", " ").title()
            ))

            synced += 1

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "success": True,
        "templates_synced": synced
    })


@app.route("/admin/templates")
def admin_templates_page():
    return render_template("admin_templates.html")


# ==========================================
# 🟢 TAG MANAGEMENT ROUTES
# ==========================================

@app.route("/tags", methods=["GET", "POST"])
@login_required
def manage_tags():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)

    # Create a new Tag
    if request.method == "POST":
        data = request.json
        name = data.get("name")
        color = data.get("color", "#00a884")
        try:
            cur.execute("INSERT INTO tags (name, color) VALUES (%s, %s)", (name, color))
            conn.commit()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": "Tag likely exists"}), 400
        finally:
            cur.close(); conn.close()

    # List all Tags
    cur.execute("SELECT * FROM tags ORDER BY name")
    tags = cur.fetchall()
    cur.close(); conn.close()
    return jsonify(tags)

@app.route("/contact_tags", methods=["GET", "POST"])
@login_required
def contact_tags_route():
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":
        # Assign or Remove a tag from a user
        data = request.json
        phone = normalize_phone(data.get("phone"))
        tag_id = data.get("tag_id")
        action = data.get("action") # 'add' or 'remove'

        if action == 'add':
            cur.execute("INSERT INTO contact_tags (contact_phone, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (phone, tag_id))
        elif action == 'remove':
            cur.execute("DELETE FROM contact_tags WHERE contact_phone = %s AND tag_id = %s", (phone, tag_id))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})

    # Get tags for a specific user
    phone = normalize_phone(request.args.get("phone"))
    cur.execute("""
        SELECT t.id, t.name, t.color
        FROM tags t
        JOIN contact_tags ct ON t.id = ct.tag_id
        WHERE ct.contact_phone = %s
    """, (phone,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify(rows)




@app.route("/automation/execute", methods=["POST"])
def automation_execute():
    auto_log("🚀 /automation/execute hit")

    try:
        data = request.get_json(silent=True) or {}
        auto_log(f"📥 Payload received: {data}")

        phone = data.get("phone")
        intent = data.get("intent")

        auto_log(f"📞 Phone raw: {phone}")
        auto_log(f"🧠 Intent: {intent}")

        if not phone or not intent:
            auto_log("❌ Missing phone or intent")
            return jsonify({"error": "missing phone or intent"}), 400

        phone = normalize_phone(phone)
        auto_log(f"📞 Phone normalized: {phone}")

        if not phone:
            auto_log("❌ Phone normalization failed")
            return jsonify({"error": "invalid phone"}), 400

        auto_log("🔌 Opening DB connection")
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        try:
            auto_log("🧩 Importing run_automations")
            from app.plugins.automations import run_automations

            auto_log("▶️ Running automations")
            run_automations(
                cur=cur,
                phone=phone,
                message_text=intent,
                send_text=send_text_internal,
                send_attachment=None
            )


            auto_log("💾 Committing DB")
            conn.commit()

        except Exception as e:
            auto_log("🔥 ERROR inside automation execution")
            traceback.print_exc()
            conn.rollback()
            return jsonify({"error": str(e)}), 500

        finally:
            auto_log("🔒 Closing DB")
            cur.close()
            conn.close()

        auto_log("✅ Automation executed successfully")
        return jsonify({"success": True})

    except Exception as e:
        auto_log("💥 FATAL ERROR in /automation/execute")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




@app.route("/automation/preview", methods=["POST"])
@login_required
def automation_preview():
    data = request.json
    message = data.get("message", "")

    from app.plugins.automations import preview_automation
    preview = preview_automation(message)

    return jsonify(preview or {})

def send_text_internal(phone, text):
    send_text_via_meta_and_db(phone, text)



    print("[AUTOMATION] send_text_internal →", resp.status_code, resp.text)

    if resp.status_code != 200:
        raise Exception("send_text_internal failed")
