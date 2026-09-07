import os
import requests
from datetime import datetime

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

try:
    response = requests.get(
        "https://pinetswap.app/",
        timeout=30
    )

    status = response.status_code
    size = len(response.text)

    message = f"""
🚀 SDA Scanner

Čas:
{datetime.now()}

PinetSwap:
✅ ONLINE

HTTP:
{status}

Velikost stránky:
{size} znaků

Scanner běží správně.
"""

except Exception as e:

    message = f"""
🚀 SDA Scanner

❌ Chyba

{str(e)}
"""

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)

print("Done")
