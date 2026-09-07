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

    whale_data = {}

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

        # IMPORTANT: The API returns the latest transactions on every run.
        # Only process a transaction once, otherwise the same whale trade
        # would be added again every 15 minutes.
        if tx_hash in seen_hashes:
            continue

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

        new_transactions += 1
        new_hashes.add(tx_hash)

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

    flow_alerts = []

    for token_address, whale in whale_data.items():

        buy_volume = whale["whale_buy_volume"]
        sell_volume = whale["whale_sell_volume"]

        net_flow = (
            buy_volume -
            sell_volume
        )

        if abs(net_flow) < 10000:
            continue

        flow_alerts.append({
            "address": token_address,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "net_flow": net_flow,
            "buy_count": whale["whale_buy_count"],
            "sell_count": whale["whale_sell_count"]
        })

    debug_message = (
        "🐋 WHALE DEBUG\n\n"
        f"Loaded transactions: {len(transactions)}\n"
        f"New transactions: {new_transactions}\n"
        f"Tracked tokens: {len(whale_data)}\n"
        f"Flow alerts: {len(flow_alerts)}"
    )

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": debug_message
        },
        timeout=30
    )

    if flow_alerts:

        flow_alerts.sort(
            key=lambda x: abs(x["net_flow"]),
            reverse=True
        )

        message = "🐋 WHALE FLOW\n\n"

        for item in flow_alerts[:10]:

            if item["net_flow"] > 0:
                signal = "🟢 WHALE ACCUMULATION"
            else:
                signal = "🔴 WHALE DISTRIBUTION"

            message += (
                f"{signal}\n"
                f"{item['address'][:12]}...\n"
                f"Buy: {item['buy_volume']:.0f} SDA\n"
                f"Sell: {item['sell_volume']:.0f} SDA\n"
                f"Net Flow: {item['net_flow']:.0f} SDA\n"
                f"Whales: "
                f"{item['buy_count']}/"
                f"{item['sell_count']}\n\n"
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
