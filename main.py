import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "Content-Type": "application/json",
    "Content-Profile": "public"
}

try:
    response = requests.post(
        URL,
        headers=HEADERS,
        json={},
        timeout=30
    )

    data = response.json()

    candidates = []

    for token in data:
        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

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
        key=lambda x: x["strength"] * x["total"],
        reverse=True
    )

    top = candidates[:5]

    message = "🚀 SDA SCANNER\n\n"

    for idx, token in enumerate(top, start=1):
        message += (
            f"{idx}. {token['address'][:12]}...\n"
            f"Buy: {token['buy']:.0f} SDA\n"
            f"Sell: {token['sell']:.0f} SDA\n"
            f"Strength: {token['strength']:.2f}\n\n"
        )

except Exception as e:
    message = f"❌ ERROR\n\n{str(e)}"

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)

print("Done")
