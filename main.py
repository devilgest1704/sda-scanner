import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

response = requests.post(
    URL,
    headers=headers,
    json={},
    timeout=30
)

message = response.text[:3500]
``

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    }
)

print("Done")
