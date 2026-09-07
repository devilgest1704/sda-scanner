import os
import requests
import traceback

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

try:

    URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

    headers = {
        "apikey": "sb_publishable_fl6m94CTRdZESg1licW9Qw_BuLlkm1Z"
    }

    response = requests.post(
        URL,
        headers=headers,
        json={},
        timeout=30
    )

    message = (
        f"HTTP: {response.status_code}\n\n"
        f"{response.text[:2000]}"
    )

except Exception:

    message = traceback.format_exc()

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    }
)

print("Done")
