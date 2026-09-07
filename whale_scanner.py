import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = (
    "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
    "?select=token_address,volume_in_sda,price_in_sda,tx_timestamp,tx_type"
    "&volume_in_sda=gte.5000"
    "&order=tx_timestamp.desc"
    "&limit=20"
)

HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public"
}

response = requests.get(
    URL,
    headers=HEADERS,
    timeout=30
)

data = response.json()

message = "🐋 WHALE SCANNER\n\n"

for tx in data[:10]:

    message += (
        f"{tx['token_address'][:12]}...\n"
        f"Volume: {float(tx['volume_in_sda']):.0f} SDA\n"
        f"Price: {float(tx['price_in_sda']):.2f}\n"
        f"Type: {tx['tx_type']}\n\n"
    )

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    },
    timeout=30
)

print("Done")
