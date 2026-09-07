import os
import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

url = "https://pinetswap.app/"

html = requests.get(url, timeout=30).text

message = f"""🚀 SDA Scanner

PinetSwap je dostupný.
Délka stránky: {len(html)} znaků

Scanner běží.
"""

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    }
)

print("done")
``