import json
import os
import openai

openai.api_key = os.getenv("OPENAI_API_KEY")

SYSTEM_PROMPT = """
You are a design assistant for greeting cards and envelopes.

Rules:
- NEVER return partial edits.
- ALWAYS return FULL final text for affected blocks.
- Do NOT invent new text.
- Preserve unchanged blocks exactly.
- If user intent is unclear, set confidence < 0.7.

Return JSON ONLY in this format:
{
  "intent": "text_change" | "no_change",
  "confidence": 0.0-1.0,
  "final_text": {
    "text1": string|null,
    "text2": string|null,
    "extra_information": array
  },
  "touched_blocks": []
}
"""

def llm_resolve_text(user_message, current_text):
    """
    current_text = {
        "text1": "...",
        "text2": "...",
        "extra_information": [...]
    }
    """

    prompt = f"""
CURRENT TEXT:
{json.dumps(current_text, ensure_ascii=False)}

USER MESSAGE:
{user_message}
"""

    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
    )

    raw = response.choices[0].message.content.strip()

    try:
        return json.loads(raw)
    except Exception:
        return {
            "intent": "no_change",
            "confidence": 0.0,
            "final_text": current_text,
            "touched_blocks": []
        }
