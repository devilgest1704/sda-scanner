import os
import json
import traceback
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


def get_signal(score, strength, trade_ratio, volume):

    if (
        score >= 3.0
        and strength >= 1.40
        and trade_ratio >= 1.20
        and volume >= 15000
    ):
        return "🚀 ACCUMULATE AGGRESSIVE"

    elif (
        score >= 2.0
        and strength >= 1.15
        and trade_ratio >= 1.00
        and volume >= 10000
    ):
        return "🟢 ACCUMULATE"

    elif score >= 1.2:
        return "🟡 HOLD"

    elif (
        score >= 0.8
        and strength < 1.0
    ):
        return "💰 TAKE PROFIT"

    else:
        return "🔴 DISTRIBUTION"


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

    print(f"Loaded tokens: {len(data)}")

    alerts = []
    valid_tokens = 0

    IMPORTANT_SIGNALS = [
        "🚀 ACCUMULATE AGGRESSIVE",
        "🟢 ACCUMULATE",
        "💰 TAKE PROFIT",
        "🔴 DISTRIBUTION"
    ]

    for token in data:

        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

        buy_count = int(token.get("buy_count", 0))
        sell_count = int(token.get("sell_count", 0))

        if total < 10000:
            continue

        valid_tokens += 1

        strength = buy / max(sell, 1)
        trade_ratio = buy_count / max(sell_count, 1)

        if total >= 20000:
            volume_bonus = 1.5
        elif total >= 10000:
            volume_bonus = 1.3
        else:
            volume_bonus = 1.0

        score = (
            (strength * 0.7) +
            (trade_ratio * 0.3)
        ) * volume_bonus

        signal = get_signal(
            score,
            strength,
            trade_ratio,
            total
        )

        address = token["token_address"]

        current_state[address] = signal

        old_signal = previous_state.get(address)

        if old_signal is None:
            continue

        old_signal = old_signal.strip()

        if old_signal == signal:
            continue

        if signal not in IMPORTANT_SIGNALS:
            continue

        alerts.append({
            "address": address,
            "old": old_signal,
            "new": signal,
            "score": score,
            "strength": strength,
            "trade_ratio": trade_ratio,
            "volume_bonus": volume_bonus,
            "volume": total,
            "buy": buy,
            "sell": sell,
            "buy_count": buy_count,
            "sell_count": sell_count
        })

    print(f"Valid tokens: {valid_tokens}")
    print(f"Signal changes: {len(alerts)}")

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": (
                "📊 SDA DEBUG\n\n"
                f"Loaded tokens: {len(data)}\n"
                f"Valid tokens: {valid_tokens}\n"
                f"Signal changes: {len(alerts)}"
            )
        },
        timeout=30
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
                f"Signal:\n"
                f"{alert['old']} → {alert['new']}\n\n"
                f"Score: {alert['score']:.2f}\n"
                f"Strength: {alert['strength']:.2f}\n"
                f"Trade Ratio: {alert['trade_ratio']:.2f}\n"
                f"Volume Bonus: {alert['volume_bonus']:.2f}\n\n"
                f"Výpočet:\n"
                f"(({alert['strength']:.2f} × 0.7) + "
                f"({alert['trade_ratio']:.2f} × 0.3)) × "
                f"{alert['volume_bonus']:.2f}\n\n"
                f"Trades: {alert['buy_count']}/{alert['sell_count']}\n"
                f"Buy: {alert['buy']:.0f} SDA\n"
                f"Sell: {alert['sell']:.0f} SDA\n"
                f"Volume: {alert['volume']:.0f} SDA\n\n"
            )

        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message[:4000]
            },
            timeout=30
        )

except Exception:

    error_text = traceback.format_exc()

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": (
                "❌ SCANNER ERROR\n\n"
                f"{error_text[:3500]}"
            )
        },
        timeout=30
    )

    print(error_text)
