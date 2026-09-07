import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

try:
    response = requests.post(URL, timeout=30)

    message = f"""
HTTP: {response.status_code}

TYPE:
{type(response.text)}

FIRST 1000 CHARS:

{response.text[:1000]}
"""

except Exception as e:
    message = str(e)

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    }
)
