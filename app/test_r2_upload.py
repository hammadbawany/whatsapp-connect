import os
import requests
from r2_client import get_r2_client

# 🔴 TEMP: put a REAL WhatsApp media_id here
MEDIA_ID = "PUT_REAL_MEDIA_ID_HERE"

def upload_one_media():
    token = os.environ.get("WHATSAPP_TOKEN")  # temp, replace if needed

    # 1️⃣ Get media URL from Meta
    meta = requests.get(
        f"https://graph.facebook.com/v20.0/{MEDIA_ID}",
        headers={"Authorization": f"Bearer {token}"}
    ).json()

    media_url = meta.get("url")
    if not media_url:
        raise Exception("Media URL not found")

    # 2️⃣ Download media
    media_resp = requests.get(
        media_url,
        headers={"Authorization": f"Bearer {token}"}
    )

    # 3️⃣ Upload to R2
    r2 = get_r2_client()
    key = f"media/voice/{MEDIA_ID}.ogg"

    r2.put_object(
        Bucket=os.environ["R2_BUCKET"],
        Key=key,
        Body=media_resp.content,
        ContentType="audio/ogg"
    )

    print("Uploaded to R2:", key)

if __name__ == "__main__":
    upload_one_media()
