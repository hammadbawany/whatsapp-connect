import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import sqlite3

app = Flask(__name__)

VERIFY_TOKEN = "lifafay123"  # choose your own
WHATSAPP_TOKEN = os.getenv("WA_TOKEN")  # permanent token
PHONE_NUMBER_ID = os.getenv("WA_PHONE")  # from Meta dashboard

DB = "chat.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_phone TEXT,
        sender TEXT,
        message TEXT,
        media_url TEXT,
        timestamp TEXT
    )""")
    conn.commit()
    conn.close()

# ------------------------------------------------------------------
# VERIFY WEBHOOK SETUP (Facebook verification)
# ------------------------------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

# ------------------------------------------------------------------
# RECEIVE MESSAGES FROM WHATSAPP (incoming)
# ------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    try:
        entry = data["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages", [])

        if messages:
            msg = messages[0]
            phone = msg["from"]
            text = msg.get("text", {}).get("body", "")
            media_url = None

            # save message
            conn = sqlite3.connect(DB)
            c = conn.cursor()
            c.execute("INSERT INTO messages (user_phone, sender, message, media_url, timestamp) VALUES (?, ?, ?, ?, ?)",
                      (phone, "customer", text, media_url, datetime.now().isoformat()))
            conn.commit()
            conn.close()

    except Exception as e:
        print("Error:", e)

    return "OK", 200

# ------------------------------------------------------------------
# SEND TEXT MESSAGE (agent → customer)
# ------------------------------------------------------------------
@app.route("/send_text", methods=["POST"])
def send_text():
    data = request.json
    phone = data["phone"]
    text = data["text"]

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    r = requests.post(url, json=payload, headers=headers)
    return jsonify(r.json())

# ------------------------------------------------------------------
# SEND PNG MESSAGE (agent → customer)
# ------------------------------------------------------------------
@app.route("/send_media", methods=["POST"])
def send_media():
    data = request.json
    phone = data["phone"]
    image_url = data["url"]

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": { "link": image_url }
    }

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    r = requests.post(url, json=payload, headers=headers)
    return jsonify(r.json())

# ------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    app.run(port=5000, debug=True)
