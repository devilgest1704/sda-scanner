import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

headers = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "Content-Type": "application/json",
    "Content-Profile": "public"
}

try:

    response = requests.post(
        URL,
        headers=headers,
        json={},
        timeout=30
    )

    message = (
        f"HTTP: {response.status_code}\n\n"
        f"{response.text[:3000]}"
    )

except Exception as e:

    message = str(e)

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    }
)

print("Done")
