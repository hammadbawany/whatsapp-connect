import os
import sys
import urllib.parse  # <--- Added this
from flask import Blueprint, request, url_for, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from db import get_conn

voice_bp = Blueprint("voice", __name__)

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_PHONE_NUMBER")
DOMAIN_URL = os.getenv("DOMAIN_URL")

# Verify Env Vars on Load
print(f"--- VOICE BOT LOADED ---")
print(f"TWILIO_SID: {TWILIO_SID}")
print(f"TWILIO_FROM: {TWILIO_FROM}")
print(f"DOMAIN_URL: {DOMAIN_URL}")

try:
    client = Client(TWILIO_SID, TWILIO_TOKEN)
except Exception as e:
    print(f"❌ Twilio Client Init Error: {e}")

# --- 1. START CALL API ---
@voice_bp.route("/api/make_call", methods=['POST'])
def make_call():
    data = request.get_json()
    phone = data.get('phone')
    name = data.get('name', 'Customer')
    code = data.get('order_code')

    print(f"\n📞 [CALL REQUEST] Starting...")
    print(f"   - Phone: {phone}")
    print(f"   - Name: {name}")

    if not phone:
        print("❌ Error: No phone provided")
        return jsonify({"error": "No phone"}), 400

    # Clean phone
    clean_phone = phone.replace(" ", "").replace("-", "")
    if clean_phone.startswith("03"): clean_phone = "+92" + clean_phone[1:]
    elif not clean_phone.startswith("+"): clean_phone = "+" + clean_phone

    print(f"   - Normalized Phone: {clean_phone}")

    # CHECK DOMAIN URL
    if not DOMAIN_URL or "127.0.0.1" in DOMAIN_URL or "localhost" in DOMAIN_URL:
        err_msg = f"❌ CRITICAL ERROR: DOMAIN_URL is '{DOMAIN_URL}'. Twilio cannot reach localhost. Use Ngrok."
        print(err_msg)
        return jsonify({"error": err_msg}), 500

    try:
        # Construct Webhook URL SAFELY
        base = DOMAIN_URL.rstrip('/')
        webhook_path = "/voice/webhook"

        # 🟢 FIX: URL Encode the parameters to handle spaces
        params = {'name': name, 'code': code}
        query_string = urllib.parse.urlencode(params)

        webhook_url = f"{base}{webhook_path}?{query_string}"
        status_callback = f"{base}/voice/status_callback"

        print(f"   - Webhook URL: {webhook_url}")

        call = client.calls.create(
            to=clean_phone,
            from_=TWILIO_FROM,
            url=webhook_url,
            status_callback=status_callback,
            status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
            machine_detection='DetectMessageEnd'
        )

        print(f"✅ Call Initiated! SID: {call.sid}")

        # Log to DB
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_logs (phone, customer_name, order_code, call_sid, status)
            VALUES (%s, %s, %s, %s, 'queued')
        """, (clean_phone, name, code, call.sid))
        conn.commit(); cur.close(); conn.close()

        return jsonify({"success": True, "call_sid": call.sid})

    except Exception as e:
        print(f"❌ TWILIO API ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# --- 2. CONVERSATION FLOW ---

@voice_bp.route("/voice/webhook", methods=['POST'])
def voice_start():
    print(f"\n🤖 [WEBHOOK] Voice Start Hit!")
    name = request.args.get('name', '')
    print(f"   - Customer: {name}")

    resp = VoiceResponse()

    if request.form.get('AnsweredBy') == 'machine_start':
        print("   - Voicemail detected. Leaving message.")
        resp.say("Asalam o Alaikum. Ye Lifafay dot pk se call thi. Hum baad mein call karein ge.")
        return str(resp)

    # Use Polly.Aditi for Urdu/Hindi accent
    # Pass current query params forward to next step
    next_url = url_for('voice.handle_identity', _external=True) + f"?{request.query_string.decode()}"

    gather = Gather(num_digits=1, action=next_url, timeout=5)

    gather.say(f"Asalam o Alaikum. Kya meri baat {name} se ho rahi hai?", voice='Polly.Aditi', language='en-IN')
    gather.say("Agar jee haan, to 1 dabayein.", voice='Polly.Aditi', language='en-IN')
    gather.say("Agar nahi, to 2 dabayein.", voice='Polly.Aditi', language='en-IN')

    resp.append(gather)
    resp.say("Hum koi input receive nahi kar sake. Allah Hafiz.", voice='Polly.Aditi', language='en-IN')

    return str(resp)

@voice_bp.route("/voice/handle_identity", methods=['POST'])
def handle_identity():
    digits = request.form.get('Digits')
    print(f"\n🤖 [WEBHOOK] Handle Identity. Input: {digits}")

    resp = VoiceResponse()

    # Preserve params for next steps
    qs = request.query_string.decode()

    if digits == '1':
        next_url = url_for('voice.handle_order_verify', _external=True) + f"?{qs}"
        gather = Gather(num_digits=1, action=next_url)
        gather.say("Shukriya. Main Ahmed baat kar raha hun Lifafay dot pk se.", voice='Polly.Aditi', language='en-IN')
        gather.say("Humein aap ki taraf se order masool hua hai.", voice='Polly.Aditi', language='en-IN')
        gather.say("Kya ye order aap ne place kiya hai? Haan ke liye 1 dabayein.", voice='Polly.Aditi', language='en-IN')
        resp.append(gather)

    elif digits == '2':
        next_url = url_for('voice.handle_fraud_check', _external=True) + f"?{qs}"
        gather = Gather(num_digits=1, action=next_url)
        gather.say("Humein aap ke number se humari website par order mila hai.", voice='Polly.Aditi', language='en-IN')
        gather.say("Kya ye order aap ne kiya hai? 1 dabayein.", voice='Polly.Aditi', language='en-IN')
        gather.say("Agar nahi, to cancel karne ke liye 2 dabayein.", voice='Polly.Aditi', language='en-IN')
        resp.append(gather)

    return str(resp)

@voice_bp.route("/voice/handle_order_verify", methods=['POST'])
def handle_order_verify():
    digits = request.form.get('Digits')
    phone = request.form.get('To')
    print(f"\n🤖 [WEBHOOK] Order Verify. Input: {digits}")

    resp = VoiceResponse()

    if digits == '1':
        msg = (
            "Zabardast. WhatsApp ki nayi policy ke mutabiq, hum customer ko pehle message nahi kar sakte. "
            "Bara-e-meherbani, aap humein WhatsApp par 'Hi' likh kar bhejein taa-ke hum aapka design shuru kar sakein. "
            "Humara number website par maujood hai, aur hum aapko SMS bhi bhej rahe hain. "
            "Shukriya, Allah Hafiz."
        )
        resp.say(msg, voice='Polly.Aditi', language='en-IN')
        send_sms(phone)
        update_call_outcome(request.form.get('CallSid'), 'confirmed_order')

    else:
        resp.say("Order cancel karne ke liye shukriya. Allah Hafiz.", voice='Polly.Aditi', language='en-IN')
        update_call_outcome(request.form.get('CallSid'), 'cancelled_order')

    return str(resp)

@voice_bp.route("/voice/handle_fraud_check", methods=['POST'])
def handle_fraud_check():
    digits = request.form.get('Digits')
    code = request.args.get('code')
    print(f"\n🤖 [WEBHOOK] Fraud Check. Input: {digits}")

    resp = VoiceResponse()

    if digits == '2':
        cancel_order_db(code)
        resp.say("Aap ka shukriya. Hum ye fake order cancel kar rahe hain. Mazrat khwah hain.", voice='Polly.Aditi', language='en-IN')
        update_call_outcome(request.form.get('CallSid'), 'cancelled_fake_order')
    else:
        # Redirect back to verify logic
        qs = request.query_string.decode()
        resp.redirect(url_for('voice.handle_order_verify', _external=True) + f"?{qs}")

    return str(resp)

@voice_bp.route("/voice/status_callback", methods=['POST'])
def status_callback():
    sid = request.form.get('CallSid')
    status = request.form.get('CallStatus')
    print(f"📡 [CALLBACK] Call {sid} is now {status}")

    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE call_logs SET status = %s WHERE call_sid = %s", (status, sid))
    conn.commit(); cur.close(); conn.close()
    return "OK", 200

# --- HELPERS ---
def send_sms(to_phone):
    try:
        print(f"📩 Sending SMS to {to_phone}")
        client.messages.create(
            body="ACTION REQUIRED: Please WhatsApp us to start your design.\nLink: https://wa.me/923001234567",
            from_=TWILIO_FROM,
            to=to_phone
        )
    except Exception as e: print(f"SMS Error: {e}")

def cancel_order_db(code):
    if not code: return
    print(f"🚫 Cancelling Order {code}...")
    conn = get_conn(); cur = conn.cursor()
    # Note: Ensure 'Orders' table exists, or wrap in try/except
    try:
        cur.execute("UPDATE Orders SET order_status='Cancelled', order_status_id=9, updated_by='System - Call Bot' WHERE order_code=%s", (code,))
        conn.commit()
    except Exception as e: print(f"DB Cancel Error: {e}")
    cur.close(); conn.close()

def update_call_outcome(sid, outcome):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE call_logs SET outcome = %s WHERE call_sid = %s", (outcome, sid))
    conn.commit(); cur.close(); conn.close()
