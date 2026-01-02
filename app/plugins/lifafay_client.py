import requests
import sys
import os

LIFAFAY_API_URL = os.getenv(
    "LIFAFAY_API_URL",
    "https://lifafay.herokuapp.com/api/design/action"
)

def llog(msg):
    print(f"[LIFAFAY-CALL] {msg}", file=sys.stdout)
    sys.stdout.flush()


def send_to_lifafay(payload: dict):
    llog("Sending payload to Lifafay")
    llog(payload)

    resp = requests.post(
        LIFAFAY_API_URL,
        json=payload,
        timeout=15
    )

    llog(f"Status → {resp.status_code}")
    llog(f"Response → {resp.text}")

    return resp.ok
