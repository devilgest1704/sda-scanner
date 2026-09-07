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

    data = response.json()

    candidates = []

    for token in data:

        buy = float(token["buy_vol_sda"])
        sell = float(token["sell_vol_sda"])
        total = float(token["total_vol_sda"])

        if total < 2000:
            continue

        strength = buy / max(sell, 1)

        candidates.append({
            "address": token["token_address"],
            "buy": buy,
            "sell": sell,
            "total": total,
            "strength": strength
        })

    candidates.sort(
        key=lambda x: (
            x["strength"],
            x["total"]
        ),
        reverse=True
    )

    top = candidates[:5]

    message = "🚀 SDA SCANNER\n\n"

    for i, token in enumerate(top, start=1):

        message += (
            f"#{i}\n"
            f"{token['address'][:12]}...\n"
            f"Buy: {token['buy']:.0f} SDA\n"
            f"Sell: {token['sell']:.0f} SDA\n"
            f"Strength: {token['strength']:.2f}\n\n"
        )

except Exception as e:

    message = f"❌ ERROR\n\n{e}"

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)
``
