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

        buy_count = int(token.get("buy_count", 0))
        sell_count = int(token.get("sell_count", 0))

        # ignoruj malé objemy
        if total < 1000:
            continue

        strength = buy / max(sell, 1)

        trade_ratio = (
            buy_count /
            max(sell_count, 1)
        )

        if total >= 10000:
            volume_bonus = 1.5
        elif total >= 5000:
            volume_bonus = 1.3
        elif total >= 1000:
            volume_bonus = 1.0
        else:
            volume_bonus = 0.7

        score = (
            (strength * 0.7) +
            (trade_ratio * 0.3)
        ) * volume_bonus

        if score >= 3.5:
            signal = "🚀 STRONG BUY"
        elif score >= 2.0:
            signal = "🟢 BUY"
        elif score >= 1.0:
            signal = "🟡 HOLD"
        elif score >= 0.6:
            signal = "🟠 WEAK SELL"
        else:
            signal = "🔴 STRONG SELL"

        candidates.append({
            "address": token["token_address"],
            "buy": buy,
            "sell": sell,
            "total": total,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "strength": strength,
            "trade_ratio": trade_ratio,
            "score": score,
            "signal": signal
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    top = candidates[:10]

    message = "🚀 SDA SCANNER\n\n"

    for idx, token in enumerate(top, start=1):

        message += (
            f"{idx}. {token['signal']}\n"
            f"{token['address'][:12]}...\n"
            f"Score: {token['score']:.2f}\n"
          
