# app/plugins/confirmation.py

import sys
import requests
import os

def log(title, payload):
    print(f"[CONFIRMATION] {title}: {payload}")
    sys.stdout.flush()

def is_design_rejection(text: str):
    """
    Returns True if the text implies a correction, rejection, or cancellation.
    """
    t = text.lower().strip()
    negatives = [
        "cancel", "change", "correction", "wrong", "mistake",
        "not approved", "not good", "bad", "issue", "error",
        "stop", "refund", "return", "don't", "dont", "wait"
    ]
    return any(n in t for n in negatives)

def is_design_confirmation(text: str):
    """
    Returns True if text implies approval.
    Explicitly excludes 'confirm order' to avoid template button collisions.
    """
    t = text.lower().strip()

    # 🚨 CRITICAL: Ignore "Confirm Order" button click text
    if "confirm order" in t:
        return False

    confirmations = [
        "ok", "okay", "done", "confirmed", "confirm",
        "approved", "perfect", "looks good", "this is fine",
        "yes", "good", "final", "go ahead", "approve", "locked"
    ]

    # Exact match or substring match
    return any(c == t or c in t for c in confirmations)

def tag_whatsapp_chat_db(cur, conn, phone, tag_id):
    """
    Directly tags the chat in the DB (faster than calling the API endpoint).
    """
    try:
        cur.execute("""
            INSERT INTO contact_tags (contact_phone, tag_id)
            VALUES (%s, %s)
            ON CONFLICT (contact_phone, tag_id) DO NOTHING
        """, (phone, tag_id))
        conn.commit()
        log("TAG APPLIED", f"Phone: {phone}, Tag: {tag_id}")
    except Exception as e:
        log("TAG ERROR", str(e))

def process_design_confirmation(cur, conn, phone, text, context_whatsapp_id):
    """
    Main logic to handle design confirmations.
    Returns: True if the message was a confirmation (handled), False otherwise.
    """

    # 1. Check for REJECTION/CANCELLATION first
    if is_design_rejection(text):
        log("🛑 DESIGN REJECTION / CANCELLATION DETECTED", text)
        # We return False so it can be processed as a normal message (agent sees the complaint)
        return False

    # 2. Check for CONFIRMATION words
    if is_design_confirmation(text):

        target_image_id = None

        # -------------------------------------------------
        # SCENARIO A: User Replied to a Specific Message
        # -------------------------------------------------
        if context_whatsapp_id:
            # Fetch details of the AGENT message being replied to
            cur.execute("""
                SELECT id, media_type, message, template_name
                FROM messages
                WHERE whatsapp_id = %s
            """, (context_whatsapp_id,))
            replied_msg = cur.fetchone()

            if replied_msg:
                # 🛑 IGNORE if replying to Order Confirmation Template
                # If template_name exists OR message text looks like an order summary
                msg_body = str(replied_msg.get('message', '')).lower()
                if replied_msg.get('template_name') or "order number" in msg_body:
                    log("⚠️ User replied 'confirm' to an Order - IGNORING DESIGN TAG", text)
                    return False

                # ✅ CASE: Replying directly to an IMAGE
                if replied_msg['media_type'] == 'image':
                    target_image_id = context_whatsapp_id

                # ✅ CASE: Replying to the "Please confirm text..." text message
                # We assume the image associated with this text is the LAST image sent before this text.
                elif "confirm text and design" in msg_body:
                    cur.execute("""
                        SELECT whatsapp_id
                        FROM messages
                        WHERE user_phone = %s
                          AND sender = 'agent'
                          AND media_type = 'image'
                          AND id < %s  -- Image must be sent BEFORE the text prompt
                        ORDER BY id DESC
                        LIMIT 1
                    """, (phone, replied_msg['id']))
                    prev_img = cur.fetchone()
                    if prev_img:
                        target_image_id = prev_img['whatsapp_id']

        # -------------------------------------------------
        # SCENARIO B: No Context (Direct Message)
        # -------------------------------------------------
        else:
            # Fetch the VERY LAST message sent by the AGENT
            cur.execute("""
                SELECT id, media_type, message, template_name, whatsapp_id
                FROM messages
                WHERE user_phone = %s AND sender = 'agent'
                ORDER BY id DESC
                LIMIT 1
            """, (phone,))
            last_agent_msg = cur.fetchone()

            if last_agent_msg:
                msg_body = str(last_agent_msg.get('message', '')).lower()

                # 🛑 IGNORE if last message was Order Template
                if last_agent_msg.get('template_name') or "order number" in msg_body:
                    log("⚠️ Last agent msg was Template - Ignoring 'No Context' Confirm", text)
                    return False

                # ✅ CASE: Last message was an IMAGE
                if last_agent_msg['media_type'] == 'image':
                    target_image_id = last_agent_msg['whatsapp_id']

                # ✅ CASE: Last message was "Please confirm text..."
                elif "confirm text and design" in msg_body:
                    cur.execute("""
                        SELECT whatsapp_id
                        FROM messages
                        WHERE user_phone = %s
                          AND sender = 'agent'
                          AND media_type = 'image'
                          AND id < %s
                        ORDER BY id DESC
                        LIMIT 1
                    """, (phone, last_agent_msg['id']))
                    prev_img = cur.fetchone()
                    if prev_img:
                        target_image_id = prev_img['whatsapp_id']

        # -------------------------------------------------
        # EXECUTE CONFIRMATION
        # -------------------------------------------------
        if target_image_id:
            cur.execute("""
                UPDATE messages
                SET is_confirmed = TRUE,
                                    confirmed_at = NOW()
                WHERE whatsapp_id = %s
            """, (target_image_id,))

            conn.commit()

            # Add Tag 5 (Confirmed)
            tag_whatsapp_chat_db(cur, conn, phone, tag_id=5)

            log("✅ DESIGN CONFIRMED & TAGGED", {
                "phone": phone,
                "trigger": text,
                "image_wamid": target_image_id
            })
            return True

    return False
