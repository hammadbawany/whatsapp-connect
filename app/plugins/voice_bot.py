import os
import re
import urllib.parse
from datetime import datetime
from flask import Blueprint, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from app.db import get_conn

# =====================================================
# CONFIG
# =====================================================
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_PHONE_NUMBER")
DOMAIN_URL = os.getenv("DOMAIN_URL")

client = Client(TWILIO_SID, TWILIO_TOKEN)
voice_bp = Blueprint("voice", __name__)

BOT_VOICE = "Google.en-IN-Neural2-B"

# =====================================================
# UTILS
# =====================================================
def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def normalize_text(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[!?.।]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

# =====================================================
# DB LOGGING
# =====================================================
def log_conversation(call_sid, phone, order_code, stage, speaker, message, intent=None):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_conversations
            (call_sid, phone, order_code, stage, speaker, message, intent)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (call_sid, phone, order_code, stage, speaker, message, intent))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ LOG ERROR:", e)

def update_outcome(call_sid, outcome):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE call_logs SET outcome=%s WHERE call_sid=%s",
            (outcome, call_sid)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ OUTCOME ERROR:", e)

# =====================================================
# INTENT DETECTION (FAST)
# =====================================================
def analyze_intent(user_speech):
    text = normalize_text(user_speech)

    yes_keywords = [
        "haan", "haan ji", "ji", "ji haan",
        "main", "main hoon", "main hun",
        "bol raha", "bol rahi",
        "haan main", "yes",
        "haan cancel"
    ]

    no_keywords = [
        "nahi", "nahin", "no",
        "galat", "wrong",
        "didi", "bhai", "behan",
        "order nahi"
    ]

    for k in no_keywords:
        if k in text:
            return "NO"

    for k in yes_keywords:
        if k in text:
            return "YES"

    return "UNKNOWN"

# =====================================================
# SPEAK
# =====================================================
def speak(resp, text, call_sid=None, phone=None, code=None, stage=None):
    resp.say(text, voice=BOT_VOICE)
    if call_sid:
        log_conversation(call_sid, phone, code, stage, "BOT", text)

# =====================================================
# START CALL
# =====================================================
@voice_bp.route("/api/make_call", methods=["POST"])
def make_call():
    data = request.get_json(force=True)
    phone = data.get("phone")
    name = data.get("name", "Customer")
    code = data.get("order_code", "")

    clean = phone.replace(" ", "").replace("-", "")
    if clean.startswith("03"):
        clean = "+92" + clean[1:]

    params = urllib.parse.urlencode({
        "stage": "intro",
        "name": name,
        "code": code
    })

    url = f"{DOMAIN_URL.rstrip('/')}/voice/conversation?{params}"

    call = client.calls.create(
        to=clean,
        from_=TWILIO_FROM,
        url=url,
        status_callback=f"{DOMAIN_URL.rstrip('/')}/voice/status_callback",
        status_callback_event=["initiated", "ringing", "answered", "completed"]
    )

    return jsonify({"success": True, "call_sid": call.sid})

# =====================================================
# CONVERSATION LOOP
# =====================================================
@voice_bp.route("/voice/conversation", methods=["GET", "POST"])
def conversation():
    stage = request.values.get("stage", "intro")
    name = request.values.get("name", "Customer")
    code = request.values.get("code", "")
    speech = request.values.get("SpeechResult")
    call_sid = request.values.get("CallSid")
    phone = request.values.get("To")

    resp = VoiceResponse()

    def next_url(s):
        return f"{DOMAIN_URL.rstrip('/')}/voice/conversation?" + urllib.parse.urlencode({
            "stage": s,
            "name": name,
            "code": code
        })

    if speech:
        log_conversation(call_sid, phone, code, stage, "USER", speech)
        intent = analyze_intent(speech)
        log_conversation(call_sid, phone, code, stage, "BOT", f"Intent={intent}", intent)

    # ================= INTRO =================
    if stage == "intro":
        if speech:
            if intent == "YES":
                resp.redirect(next_url("verify"))
            elif intent == "NO":
                speak(resp, "Theek hai…", call_sid, phone, code, stage)
                resp.redirect(next_url("fraud_confirm"))
            else:
                speak(resp, "Maaf kijiye. Jee haan ya nahi boliye.", call_sid, phone, code, stage)
                resp.redirect(next_url("intro"))
        else:
            g = Gather(
                input="speech",
                language="en-IN",
                timeout=3,
                speechTimeout=0.5,
                speechModel="phone_call",
                action=next_url("intro")
            )
            speak(resp, f"Hello. Kya meri baat {name} se ho rahi hai?", call_sid, phone, code, stage)
            resp.append(g)

    # ================= VERIFY =================
    elif stage == "verify":
        if speech:
            if intent == "YES":
                speak(resp, "Theek hai…", call_sid, phone, code, stage)
                speak(
                    resp,
                    "Aap ka order lif faa faay dot p k par confirm ho gaya hai. "
                    "WhatsApp ki nayi policy ke mutabiq, business pehle message nahi bhej sakta. "
                    "Design approval ke liye please aap humein WhatsApp par khud message bhejein. "
                    "w w w dot lif faa faay dot p k. Shukriya.",
                    call_sid, phone, code, stage
                )
                update_outcome(call_sid, "CONFIRMED")
                resp.hangup()
            else:
                resp.redirect(next_url("fraud_confirm"))
        else:
            speak(resp, "Main lif faa faay dot p k se baat kar raha hoon.", call_sid, phone, code, stage)
            g = Gather(
                input="speech",
                language="en-IN",
                timeout=3,
                speechTimeout=0.5,
                speechModel="phone_call",
                action=next_url("verify")
            )
            speak(resp, "Kya aap ne yeh order place kiya tha?", call_sid, phone, code, stage)
            resp.append(g)

    # ================= FRAUD CONFIRM =================
    elif stage == "fraud_confirm":
        if speech:
            if intent == "YES":
                speak(resp, "Theek hai. Hum yeh order cancel kar rahe hain.", call_sid, phone, code, stage)
                update_outcome(call_sid, "CANCELLED")
                resp.hangup()
            else:
                resp.redirect(next_url("verify"))
        else:
            speak(
                resp,
                "Main lif faa fay dot p k se baat kar raha hoon. "
                "Cancel karna ho to 'haan cancel' boliye. "
                "Order aap ka ho to 'nahi' boliye.",
                call_sid, phone, code, stage
            )
            g = Gather(
                input="speech",
                language="en-IN",
                timeout=3,
                speechTimeout=0.5,
                speechModel="phone_call",
                action=next_url("fraud_confirm")
            )
            resp.append(g)

    return str(resp)

# =====================================================
# STATUS CALLBACK
# =====================================================
@voice_bp.route("/voice/status_callback", methods=["POST"])
def status_callback():
    call_sid = request.values.get("CallSid")
    status = request.values.get("CallStatus")

    if status == "completed":
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE call_logs
                SET outcome = COALESCE(outcome, 'NO_DECISION')
                WHERE call_sid=%s
            """, (call_sid,))
            conn.commit()
            cur.close()
            conn.close()
        except:
            pass

    return "OK", 200

# =====================================================
# TRANSCRIPT API (FRONTEND USES THIS)
# =====================================================
@voice_bp.route("/api/call_transcript")
def call_transcript():
    order_code = request.args.get("order_code")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT speaker, message, created_at
        FROM call_conversations
        WHERE order_code=%s
        ORDER BY created_at ASC
    """, (order_code,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify([
        {
            "speaker": r["speaker"],
            "message": r["message"],
            "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        }
        for r in rows
    ])
