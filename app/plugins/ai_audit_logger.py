def log_llm_decision(conn, phone, user_message, before_text, after_text, confidence, reason):
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_text_audit_logs (
                phone,
                user_message,
                before_text,
                after_text,
                confidence,
                decision_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            phone,
            user_message,
            json.dumps(before_text),
            json.dumps(after_text),
            confidence,
            reason
        ))
        conn.commit()
    except Exception as e:
        print("❌ Failed to log LLM decision:", e)
