import os
import json
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

URL = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/rpc/get_token_stats_24h"

HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "Content-Type": "application/json",
    "Content-Profile": "public"
}

STATE_FILE = "state.json"


def load_state():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_signal(score):

    if score >= 3.5:
        return "🚀 STRONG BUY"

    elif score >= 2.0:
        return "🟢 BUY"

    elif score >= 1.0:
        return "🟡 HOLD"

    elif score >= 0.6:
        return "🟠 WEAK SELL"

    else:
        return "🔴 STRONG SELL"


try:

    previous_state = load_state()
    current_state = {}

    response = requests.post(
        URL,
        headers=HEADERS,
        json={},
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    alerts = []

    for token in data:

        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

        buy_count = int(token.get("buy_count", 0))
        sell_count = int(token.get("sell_count", 0))

        if total < 1000:
            continue

        strength = buy / max(sell, 1)

        trade_ratio = buy_count / max(sell_count, 1)

        if total >= 10000:
            volume_bonus = 1.5
        elif total >= 5000:
            volume_bonus = 1.3
        else:
            volume_bonus = 1.0

        score = (
            (strength * 0.7) +
            (trade_ratio * 0.3)
        ) * volume_bonus

        signal = get_signal(score)

        address = token["token_address"]

        current_state[address] = signal

        old_signal = previous_state.get(address)

        # první spuštění nealertuje
        if old_signal is None:
            continue

        if old_signal != signal:

            alerts.append(
                {
                    "address": address,
                    "old": old_signal,
                    "new": signal,
                    "score": score,
                    "strength": strength,
                    "volume": total,
                    "buy": buy,
                    "sell": sell
                }
            )

    save_state(current_state)

    if alerts:

        alerts.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        message = "🔄 SDA SIGNAL CHANGES\n\n"

        for alert in alerts[:10]:

            message += (
                f"{alert['address'][:12]}...\n"
                f"{alert['old']} → {alert['new']}\n"
                f"Score: {alert['score']:.2f}\n"
                f"Strength: {alert['strength']:.2f}\n"
                f"Volume: {alert['volume']:.0f} SDA\n"
                f"Buy: {alert['buy']:.0f}\n"
                f"Sell: {alert['sell']:.0f}\n\n"
            )

        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message[:4000]
            },
            timeout=30
        )

    print(f"Signal changes: {len(alerts)}")

except Exception as e:

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": f"❌ SCANNER ERROR\n\n{str(e)}"
        },
        timeout=30
    )

    print(e)
