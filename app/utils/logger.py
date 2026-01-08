import json
from datetime import datetime
def log(event, payload=None):
    import json
    from datetime import datetime

    print("\n" + "=" * 80)
    print(f"{datetime.utcnow().isoformat()} :: {event}")

    if payload is not None:
        try:
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            print("⚠️ Log serialization failed:", e)
            print(payload)

    print("=" * 80)
