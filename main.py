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
WHALE_DATA_FILE = "whale_data.json"


def load_state():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_whale_data():
    if Path(WHALE_DATA_FILE).exists():
        with open(WHALE_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {}


try:

    previous_state = load_state()
    current_state = {}
    whale_data = load_whale_data()

    response = requests.post(
        URL,
        headers=HEADERS,
        json={},
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

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
        else:
            volume_bonus = 1.3

        score = (
            (strength * 0.7) +
            (trade_ratio * 0.3)
        ) * volume_bonus

        address = token["token_address"]

        whale = whale_data.get(address, {})

        whale_buy_volume = float(
            whale.get("whale_buy_volume", 0)
        )

        whale_sell_volume = float(
            whale.get("whale_sell_volume", 0)
        )

        whale_buy_count = int(
            whale.get("whale_buy_count", 0)
        )

        whale_sell_count = int(
            whale.get("whale_sell_count", 0)
        )

        net_whale_flow = (
            whale_buy_volume -
            whale_sell_volume
        )

        # CONFIRMED SIGNALS

        if (
            score >= 3.0
            and strength >= 1.40
            and trade_ratio >= 1.20
            and total >= 15000
            and net_whale_flow >= 20000
            and whale_buy_count >= 2
        ):

            signal = "🚀 ACCUMULATE AGGRESSIVE"

        elif (
            score >= 2.0
            and strength >= 1.15
            and trade_ratio >= 1.00
            and total >= 10000
            and net_whale_flow >= 10000
        ):

            signal = "🟢 ACCUMULATE"

        elif (
            strength < 1.0
            and net_whale_flow <= -10000
        ):

            signal = "💰 TAKE PROFIT"

        elif (
            net_whale_flow <= -30000
        ):

            signal = "🔴 DISTRIBUTION"

        else:

            signal = "🟡 HOLD"

        current_state[address] = signal

        old_signal = previous_state.get(address)

        if old_signal is None:
            continue

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
            "volume": total,
            "net_whale_flow": net_whale_flow,
            "whale_buy_volume": whale_buy_volume,
            "whale_sell_volume": whale_sell_volume,
            "whale_buy_count": whale_buy_count,
            "whale_sell_count": whale_sell_count
        })

    save_state(current_state)

    # Debug goes only to the GitHub Actions log. Telegram is used only for
    # actual signal changes, so there is no message every 15 minutes.
    print(
        "📊 SDA DEBUG\n\n"
        f"Loaded tokens: {len(data)}\n"
        f"Valid tokens: {valid_tokens}\n"
        f"Whale tokens: {len(whale_data)}\n"
        f"Signal changes: {len(alerts)}"
    )

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
                f"Volume: {alert['volume']:.0f} SDA\n\n"

                f"Net Whale Flow: "
                f"{alert['net_whale_flow']:.0f} SDA\n"

                f"Whale Buy Volume: "
                f"{alert['whale_buy_volume']:.0f} SDA\n"

                f"Whale Sell Volume: "
                f"{alert['whale_sell_volume']:.0f} SDA\n"

                f"Whale Trades: "
                f"{alert['whale_buy_count']}/"
                f"{alert['whale_sell_count']}\n\n"
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
