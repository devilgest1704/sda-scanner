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

    buy_candidates = []
    sell_candidates = []

    for token in data:

        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

        if total < 5000:
            continue

        strength = buy / max(sell, 1)

        address = token["token_address"]

        if strength >= 1.25:

            buy_candidates.append({
                "address": address,
                "buy": buy,
                "sell": sell,
                "strength": strength,
                "volume": total
            })

        elif strength <= 0.80:

            sell_candidates.append({
                "address": address,
                "buy": buy,
                "sell": sell,
                "strength": strength,
                "volume": total
            })

    buy_candidates.sort(
        key=lambda x: x["strength"] * x["volume"],
        reverse=True
    )

    sell_candidates.sort(
        key=lambda x: (1 / x["strength"]) * x["volume"],
        reverse=True
    )

    message = "🚀 SDA SCANNER\n\n"

    if buy_candidates:

        message += "🟢 BUY SIGNALS\n\n"

        for token in buy_candidates[:3]:

            message += (
                f"{token['address'][:12]}...\n"
                f"Buy: {token['buy']:.0f}\n"
                f"Sell: {token['sell']:.0f}\n"
                f"Strength: {token['strength']:.2f}\n\n"
            )

    if sell_candidates:

        message += "\n🔴 SELL SIGNALS\n\n"

        for token in sell_candidates[:3]:

            message += (
                f"{token['address'][:12]}...\n"
                f"Buy: {token['buy']:.0f}\n"
                f"Sell: {token['sell']:.0f}\n"
                f"Strength: {token['strength']:.2f}\n\n"
            )

    if not buy_candidates and not sell_candidates:

        message = (
            "📊 SDA SCANNER\n\n"
            "Žádný zajímavý BUY ani SELL signál."
        )

except Exception as e:

    message = f"❌ ERROR\n\n{str(e)}"

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message[:4000]
    },
    timeout=30
)

print("Done")
