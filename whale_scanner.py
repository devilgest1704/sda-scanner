import os
import json
import traceback
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = "whale_state.json"

URL = (
    "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
    "?select=id,tx_hash,token_address,price_in_sda,"
    "volume_in_sda,tx_timestamp,tx_type"
    "&volume_in_sda=gte.5000"
    "&order=tx_timestamp.desc"
    "&limit=50"
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


try:

    state = load_state()

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

    print(f"Loaded transactions: {len(transactions)}")

    alerts = []
    new_hashes = set(seen_hashes)

    for tx in transactions:

        tx_hash = tx.get("tx_hash")

        if not tx_hash:
            continue

        if tx_hash in seen_hashes:
            continue

        new_hashes.add(tx_hash)

        volume = float(
            tx.get("volume_in_sda", 0)
        )

        price = float(
            tx.get("price_in_sda", 0)
        )

        token_address = tx.get(
            "token_address",
            "unknown"
        )

        tx_type = tx.get(
            "tx_type",
            "unknown"
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

    save_state({
        "seen_hashes": list(new_hashes)[-1000:]
    })

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

        print(f"Whale alerts: {len(alerts)}")

    else:

        print("No new whale alerts")

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
