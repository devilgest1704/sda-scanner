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

        if total < 300:
            continue

        strength = buy / max(sell, 1)

        score = (
            strength * 50
            + min(total, 2000) / 2000 * 50
        )

        candidates.append({
            "address": token["token_address"],
            "buy": buy,
            "sell": sell,
            "total": total,
            "strength": strength,
            "score": score
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    top = candidates[:10]

    message = "🚀 SDA SCANNER\n\n"

    for idx, token in enumerate(top, start=1):

        if token["score"] >= 80:
            signal = "🟢 STRONG BUY"
        elif token["score"] >= 60:
            signal = "✅ BUY WATCH"
        elif token["score"] >= 40:
            signal = "🟡 HOLD"
        else:
            signal = "🔴 AVOID"

        message += (
            f"{idx}. {signal}\n"
            f"{token['address'][:12]}...\n"
            f"Score: {token['score']:.1f}\n"
            f"Buy: {token['buy']:.0f}\n"
            f"Sell: {token['sell']:.0f}\n"
            f"Volume: {token['total']:.0f}\n\n"
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

print("Done")
