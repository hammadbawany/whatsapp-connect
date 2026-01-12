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

    # 1. Edit Starters (Strong signals)
    edit_starters = [
        "change", "edit", "correct", "fix", "replace",
        "make", "update", "remove", "delete", "rewrite",
        "write", "set", "add", "spelling", "spell"
    ]

    # 2. Contextual Edit Words (If these appear anywhere, it's likely an edit)
    edit_keywords = [
        "font", "size", "color", "colour", "bold", "italic",
        "capital", "small", "upper", "lower", "mistake",
        "wrong", "spelling", "alignment", "center", "move"
    ]

    # Check matches
    if any(t.startswith(v) for v in edit_starters):
        return True

    if any(k in t for k in edit_keywords):
        return True

    return False


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

    # 1. Clean the text: Remove emojis and punctuation, keep only letters/numbers
    # This turns "Ok 👍" into "ok" and "Confirmed." into "confirmed"
    clean_text = re.sub(r'[^\w\s]', '', text.lower()).strip()

    # 2. Split into set of words for fast lookup
    words = set(clean_text.split())

    confirmations = {
        "confirm", "confirmed", "confirming",
        "ok", "okay", "k", "done",
        "final", "finalize", "approved", "approve",
        "perfect", "good", "great", "nice",
        "print", "printing",
        "proceed", "go", "ahead",
        "lock", "yes", "yep", "yeah", "ji",

        # Roman Urdu
        "theek", "thk", "sahi", "set",
        "haan", "han", "jee", "g",
        "kardo", "krdo", "kardain", "krden", "karden"
    }

    # 3. Check for phrase matches (for multi-word confirmations)
    phrases = [
        "looks good", "it is good", "its good",
        "all good", "go ahead", "send for printing",
        "all correct", "everything is correct" 
    ]

    # Check 1: Is any specific word in the set?
    if not words.isdisjoint(confirmations):
        return True

    # Check 2: Do any specific phrases appear in the raw text?
    if any(p in clean_text for p in phrases):
        return True

    return False



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
