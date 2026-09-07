import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

headers = {
    "apikey": "sb_publishable_fl6m94CTRdZESg1licW9Qw_BuLlkm1Z",
    "Content-Type": "application/json"
}

try:

    response = requests.post(
        URL,
        headers=headers,
        json={},
        timeout=30
    )

    data = response.json()

    message = f"""
✅ SDA Scanner

HTTP: {response.status_code}

Records:
{len(data)}

First:
{data[0]}
"""

except Exception as e:

    message = f"❌ Error\n\n{e}"

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    },
    timeout=30
)

print("Done")
