import os
import json
import traceback
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = "whale_state.json"
WHALE_DATA_FILE = "whale_data.json"

URL = (
    "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
    "?select=id,tx_hash,token_address,price_in_sda,"
    "volume_in_sda,tx_timestamp,tx_type"
    "&volume_in_sda=gte.5000"
    "&order=tx_timestamp.desc"
    "&limit=150"
)

HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public"
}


def load_state():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "seen_hashes": []
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


state = load_state()

try:

    seen_hashes = set(
        state.get("seen_hashes", [])
    )

    response = requests.get(
        URL,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    transactions = response.json()

    whale_data = {}

    alerts = []
    new_hashes = set(seen_hashes)
    new_transactions = 0

    for tx in transactions:

        tx_hash = tx.get("tx_hash")

        if not tx_hash:
            continue

        token_address = tx.get(
            "token_address",
            ""
        )

        tx_type = str(
            tx.get("tx_type", "")
        ).lower()

        volume = float(
            tx.get("volume_in_sda", 0)
        )

        if token_address not in whale_data:

            whale_data[token_address] = {
                "whale_buy_volume": 0.0,
                "whale_sell_volume": 0.0,
                "whale_buy_count": 0,
                "whale_sell_count": 0
            }

        if tx_type == "buy":

            whale_data[token_address][
                "whale_buy_volume"
            ] += volume

            whale_data[token_address][
                "whale_buy_count"
            ] += 1

        elif tx_type == "sell":

            whale_data[token_address][
                "whale_sell_volume"
            ] += volume

            whale_data[token_address][
                "whale_sell_count"
            ] += 1

        if tx_hash in seen_hashes:
            continue

        new_transactions += 1

        new_hashes.add(tx_hash)

        price = float(
            tx.get("price_in_sda", 0)
        )

        timestamp = tx.get(
            "tx_timestamp",
            ""
        )

        if volume >= 20000:
            whale_level = "🐋 MEGA WHALE"

        elif volume >= 10000:
            whale_level = "🐳 WHALE"

        else:
            whale_level = "🐟 LARGE TRADE"

        alerts.append({
            "level": whale_level,
            "address": token_address,
            "volume": volume,
            "price": price,
            "type": tx_type,
            "timestamp": timestamp
        })

    with open(
        WHALE_DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            whale_data,
            f,
            indent=2
        )

    save_state({
        "seen_hashes": list(new_hashes)[-1000:]
    })

    debug_message = (
        "🐋 WHALE DEBUG\n\n"
        f"Loaded transactions: {len(transactions)}\n"
        f"New transactions: {new_transactions}\n"
        f"Whale alerts: {len(alerts)}\n"
        f"Tracked tokens: {len(whale_data)}"
    )

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": debug_message
        },
        timeout=30
    )

    if alerts:

        alerts.sort(
            key=lambda x: x["volume"],
            reverse=True
        )

        message = "🐋 SDA WHALE ALERTS\n\n"

        for alert in alerts[:10]:

            if alert["type"] == "buy":
                direction = "🟢 WHALE BUY"

            elif alert["type"] == "sell":
                direction = "🔴 WHALE SELL"

            else:
                direction = alert["type"]

            message += (
                f"{alert['level']}\n"
                f"{alert['address'][:12]}...\n"
                f"{direction}\n"
                f"Volume: {alert['volume']:.0f} SDA\n"
                f"Price: {alert['price']:.4f}\n"
                f"Time: {alert['timestamp']}\n\n"
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
                "❌ WHALE SCANNER ERROR\n\n"
                f"{error_text[:3500]}"
            )
        },
        timeout=30
    )

    print(error_text)
