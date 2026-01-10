# app/plugins/confirmation.py

import re
import sys

def clog(msg):
    print(f"[CONFIRMATION] {msg}", file=sys.stdout)
    sys.stdout.flush()


# -------------------------------------------------
# 🔍 HARD BLOCK: TEXT EDIT COMMANDS
# -------------------------------------------------
def is_text_edit_command(text: str) -> bool:
    if not text:
        return False

    t = text.lower().strip()

    edit_starters = [
        "change", "edit", "correct", "fix", "replace",
        "make", "update", "remove", "delete", "rewrite",
        "write", "set"
    ]

    return any(t.startswith(v) for v in edit_starters)


# -------------------------------------------------
# ❌ DESIGN REJECTION / CANCELLATION
# -------------------------------------------------
def is_design_rejection(text: str) -> bool:
    if not text:
        return False

    t = text.lower().strip()

    # 🚫 ABSOLUTE BLOCK — NEVER reject on edit commands
    if is_text_edit_command(t):
        return False

    negative_phrases = [
        "cancel",
        "wrong",
        "not approved",
        "not good",
        "bad",
        "issue",
        "error",
        "problem",
        "stop",
        "refund",
        "return",
        "reject",
        "dont like",
        "do not like",
        "no print",
        "dont print"
    ]

    return any(p in t for p in negative_phrases)


# -------------------------------------------------
# ✅ DESIGN CONFIRMATION
# -------------------------------------------------
def is_design_confirmation(text: str) -> bool:
    if not text:
        return False

    t = text.lower().strip()

    confirmations = [
        "confirm",
        "confirmed",
        "ok",
        "okay",
        "done",
        "final",
        "approved",
        "perfect",
        "print",
        "go ahead",
        "proceed",
        "lock",

        # Roman Urdu
        "theek",
        "sahi",
        "haan",
        "han",
        "jee",
        "kardo",
        "kardain",
        "krden"
    ]

    return t in confirmations


# -------------------------------------------------
# 🧠 MAIN ENTRY — USED BY WEBHOOK
# -------------------------------------------------
def process_design_confirmation(cur, conn, phone, text, context_whatsapp_id):
    """
    Returns:
        True  -> Confirmation or rejection handled
        False -> Let AI / automation continue
    """

    if not text:
        return False

    clean = text.lower().strip()

    # 🚫 NEVER intercept text edits
    if is_text_edit_command(clean):
        clog(f"✏️ TEXT EDIT DETECTED — skipping confirmation: {text}")
        return False

    # ❌ REJECTION
    if is_design_rejection(clean):
        clog(f"🛑 DESIGN REJECTION / CANCELLATION DETECTED: {text}")

        try:
            cur.execute("""
                INSERT INTO design_confirmations (
                    phone,
                    status,
                    reason
                ) VALUES (%s, %s, %s)
            """, (phone, "rejected", text))
            conn.commit()
        except Exception as e:
            clog(f"DB ERROR (rejection): {e}")

        return True

    # ✅ CONFIRMATION
    if is_design_confirmation(clean):
        clog(f"✅ DESIGN CONFIRMED: {text}")

        try:
            cur.execute("""
                INSERT INTO design_confirmations (
                    phone,
                    status
                ) VALUES (%s, %s)
            """, (phone, "confirmed"))
            conn.commit()
        except Exception as e:
            clog(f"DB ERROR (confirmation): {e}")

        return True

    # 🤷 Not confirmation, not rejection
    return False
