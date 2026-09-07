import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

WATCHLIST = [
    {
        "name": "DBI",
        "score": 75,
        "change": "+4.09%"
    },
    {
        "name": "REGS",
        "score": 72,
        "change": "+1.25%"
    },
    {
        "name": "FREE",
        "score": 68,
        "change": "+0.09%"
    }
]

message = "🚀 SDA Scanner\n\n"

for idx, token in enumerate(WATCHLIST, start=1):
    message += (
        f"{idx}. {token['name']}\n"
        f"Score: {token['score']}\n"
        f"24h: {token['change']}\n\n"
    )

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

requests.post(
    url,
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)

print("Alert sent")