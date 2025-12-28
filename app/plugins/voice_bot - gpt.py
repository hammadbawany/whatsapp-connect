import os
import urllib.parse
from flask import Blueprint, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from db import get_conn

# ===============================
# CONFIG
# ===============================
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_PHONE_NUMBER")
DOMAIN_URL = os.getenv("DOMAIN_URL")

client = Client(TWILIO_SID, TWILIO_TOKEN)

voice_bp = Blueprint("voice", __name__)

# ===============================
# SIMPLE INTENT DETECTION
# ===============================
def analyze_intent(text):
    if not text:
        return "UNKNOWN"

    t = text.lower()

    if any(w in t for w in ["jee", "ji", "haan", "han", "hanji", "yes", "bilkul", "sahi", "ok"]):
        return "YES"

    if any(w in t for w in ["nahi", "na", "no", "cancel", "ghalat", "wrong"]):
        return "NO"

    return "UNKNOWN"

# ===============================
# START CALL
# ===============================
@voice_bp.route("/api/make_call", methods=["POST"])
def make_call():
    data = request.get_json()
    phone = data.get("phone")
    name = data.get("name", "Customer")
    code = data.get("order_code")

    if not phone:
        return jsonify({"error": "Phone required"}), 400

    clean = phone.replace(" ", "").replace("-", "")
    if clean.startswith("03"):
        clean = "+92" + clean[1:]
    elif not clean.startswith("+"):
        clean = "+" + clean

    base = DOMAIN_URL.rstrip("/")
    params = urllib.parse.urlencode({
        "stage": "intro",
        "name": name,
        "code": code,
        "attempt": 0
    })

    call = client.calls.create(
        to=clean,
        from_=TWILIO_FROM,
        url=f"{base}/voice/conversation?{params}",
        status_callback=f"{base}/voice/status_callback",
        status_callback_event=["initiated", "ringing", "answered", "completed"]
    )

    return jsonify({"success": True, "call_sid": call.sid})

# ===============================
# CONVERSATION
# ===============================
@voice_bp.route("/voice/conversation", methods=["GET", "POST"])
def conversation():
    stage = request.values.get("stage", "intro")
    name = request.values.get("name", "Customer")
    code = request.values.get("code")
    attempt = int(request.values.get("attempt", 0))
    speech = request.values.get("SpeechResult")
    call_sid = request.values.get("CallSid")

    resp = VoiceResponse()

    def next_url(s, a):
        base = DOMAIN_URL.rstrip("/")
        q = urllib.parse.urlencode({
            "stage": s,
            "name": name,
            "code": code,
            "attempt": a
        })
        return f"{base}/voice/conversation?{q}"

    # ===========================
    # INTRO
    # ===========================
    if stage == "intro":
        if attempt >= 2:
            resp.say(
                "Hum baad mein dobara rabta karein ge. Allah Hafiz.",
                voice="Polly.Matthew",
                language="en-US"
            )
            resp.hangup()
            return str(resp)

        if speech:
            intent = analyze_intent(speech)
            if intent == "YES":
                resp.redirect(next_url("verify", 0))
                return str(resp)
            if intent == "NO":
                resp.redirect(next_url("fraud", 0))
                return str(resp)

        g = Gather(
            input="speech",
            language="en-IN",
            timeout=2,
            speechTimeout="auto",
            bargeIn=True,
            action=next_url("intro", attempt + 1)
        )
        g.say(
            f"Assalam o Alaikum. Main Ahmed Lifafay dot pk se baat kar raha hoon. "
            f"Kya main {name} se baat kar raha hoon?",
            voice="Polly.Matthew",
            language="en-US"
        )
        resp.append(g)

        resp.say(
            "Main aap ki awaaz nahi sun saka. Main dobara pooch raha hoon.",
            voice="Polly.Matthew",
            language="en-US"
        )
        resp.redirect(next_url("intro", attempt + 1))
        return str(resp)

    # ===========================
    # VERIFY
    # ===========================
    if stage == "verify":
        if attempt >= 2:
            resp.say(
                "Hum baad mein dobara rabta karein ge. Allah Hafiz.",
                voice="Polly.Matthew",
                language="en-US"
            )
            resp.hangup()
            return str(resp)

        if speech:
            intent = analyze_intent(speech)
            if intent == "YES":
                resp.redirect(next_url("done", 0))
                return str(resp)
            if intent == "NO":
                resp.say(
                    "Theek hai. Hum yeh order cancel kar rahe hain.",
                    voice="Polly.Matthew",
                    language="en-US"
                )
                resp.hangup()
                return str(resp)

        g = Gather(
            input="speech",
            language="en-IN",
            timeout=2,
            speechTimeout="auto",
            bargeIn=True,
            action=next_url("verify", attempt + 1)
        )
        g.say(
            "Humein aap ke number se aik order mila hai. "
            "Kya yeh order aap ne place kiya tha?",
            voice="Polly.Matthew",
            language="en-US"
        )
        resp.append(g)

        resp.say(
            "Awaaz clear nahi thi. Main dobara pooch raha hoon.",
            voice="Polly.Matthew",
            language="en-US"
        )
        resp.redirect(next_url("verify", attempt + 1))
        return str(resp)

    # ===========================
    # DONE
    # ===========================
    if stage == "done":
        resp.say(
            "Shukriya. Design shuru karne ke liye please WhatsApp par humein Hi message kar dein. "
            "Humara number website par maujood hai. Allah Hafiz.",
            voice="Polly.Matthew",
            language="en-US"
        )
        resp.hangup()
        return str(resp)

    return str(resp)

# ===============================
# STATUS CALLBACK (ONLY ONCE)
# ===============================
@voice_bp.route("/voice/status_callback", methods=["POST"])
def voice_status_callback():
    print(
        "CALL STATUS:",
        request.values.get("CallSid"),
        request.values.get("CallStatus"),
        request.values.get("ErrorCode")
    )
    return "OK", 200
