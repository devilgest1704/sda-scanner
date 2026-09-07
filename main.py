import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

headers = {
    "Content-Type": "application/json"
}

try:
    response = requests.post(
        URL,
        headers=headers,
        timeout=30
    )

    data = response.json()

    candidates = []

    for token in data:

        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

        if total < 1000:
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
        key=lambda x: (x["strength"], x["total"]),
        reverse=True
    )

    top = candidates[:5]

    message = "🚀 SDA Scanner\n\n"

    for i, t in enumerate(top, start=1):

        message += (
            f"{i}. {t['address'][:10]}...\n"
            f"Buy: {round(t['buy'])} SDA\n"
            f"Sell: {round(t['sell'])} SDA\n"
            f"Strength: {round(t['strength'],2)}\n\n"
        )

except Exception as e:

    message = f"❌ Scanner error\n\n{str(e)}"

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)

print("Done")
