import json
import os
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

SUPABASE_URL = (
    "https://uhrsigapvhlpudafxqfg.supabase.co"
    "/rest/v1/token_transactions"
)

# Same working publishable key used by whale_scanner.py
SUPABASE_KEY = "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z"

MARKET_DATA_FILE = "market_data.json"

FETCH_LIMIT = 1000
MAX_HISTORY_POINTS = 20000
MIN_RECENT_VOLUME_SDA = 1000

HEADERS = {
    "apikey": SUPABASE_KEY,
    "accept-profile": "public",
}


# ============================================================
# FILE HELPERS
# ============================================================

def load_market_data():
    if not Path(MARKET_DATA_FILE).exists():
        return {"updated_at": None, "tokens": {}}

    try:
        with open(MARKET_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {"updated_at": None, "tokens": {}}

        if not isinstance(data.get("tokens"), dict):
            data["tokens"] = {}

        return data

    except Exception:
        return {"updated_at": None, "tokens": {}}


def save_market_data(data):
    tmp_file = MARKET_DATA_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    os.replace(tmp_file, MARKET_DATA_FILE)


# ============================================================
# TIME HELPERS
# ============================================================

def parse_timestamp(value):
    if not value:
        return None

    try:
        text = str(value)

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        dt = datetime.fromisoformat(text)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def now_utc():
    return datetime.now(timezone.utc)


# ============================================================
# SUPABASE
# ============================================================

def fetch_transactions():
    params = {
        "select": (
            "id,"
            "tx_hash,"
            "token_address,"
            "price_in_sda,"
            "volume_in_sda,"
            "tx_timestamp,"
            "tx_type"
        ),
        "order": "tx_timestamp.desc",
        "limit": FETCH_LIMIT,
    }

    response = requests.get(
        SUPABASE_URL,
        headers=HEADERS,
        params=params,
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Supabase HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

    if not isinstance(data, list):
        raise RuntimeError("Supabase returned unexpected data format")

    return data


# ============================================================
# TRANSACTION NORMALIZATION
# ============================================================

def normalize_transaction(tx):
    try:
        token = tx.get("token_address")
        if not token:
            return None

        price = float(tx.get("price_in_sda") or 0)
        volume = float(tx.get("volume_in_sda") or 0)
        timestamp = parse_timestamp(tx.get("tx_timestamp"))
        tx_type = str(tx.get("tx_type") or "").lower()

        if price <= 0 or volume < 0 or timestamp is None:
            return None

        if tx_type not in ("buy", "sell"):
            return None

        return {
            "hash": tx.get("tx_hash"),
            "price": price,
            "volume": volume,
            "timestamp": timestamp.isoformat(),
            "type": tx_type,
        }

    except Exception:
        return None


# ============================================================
# HISTORY
# ============================================================

def add_transaction(token_data, tx):
    history = token_data.setdefault("transactions", [])
    tx_hash = tx.get("hash")

    if tx_hash:
        for existing in history[-200:]:
            if existing.get("hash") == tx_hash:
                return False

    history.append(tx)
    return True


def get_transactions_in_window(history, current_time, minutes):
    cutoff = current_time - timedelta(minutes=minutes)
    result = []

    for tx in history:
        timestamp = parse_timestamp(tx.get("timestamp"))
        if timestamp is not None and timestamp >= cutoff:
            result.append(tx)

    return result


# ============================================================
# MARKET CALCULATIONS
# ============================================================

def calculate_window(history, current_time, minutes):
    transactions = get_transactions_in_window(
        history, current_time, minutes
    )

    buy_volume = 0.0
    sell_volume = 0.0
    buy_count = 0
    sell_count = 0

    for tx in transactions:
        volume = float(tx.get("volume", 0))

        if tx.get("type") == "buy":
            buy_volume += volume
            buy_count += 1
        elif tx.get("type") == "sell":
            sell_volume += volume
            sell_count += 1

    total_volume = buy_volume + sell_volume
    net_flow = buy_volume - sell_volume

    if sell_volume > 0:
        ratio = buy_volume / sell_volume
    elif buy_volume > 0:
        ratio = buy_volume
    else:
        ratio = 0.0

    return {
        "transactions": len(transactions),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "total_volume": total_volume,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "net_flow": net_flow,
        "buy_sell_ratio": ratio,
    }


def find_price_before(history, current_time, minutes):
    target = current_time - timedelta(minutes=minutes)

    best = None
    best_difference = None

    for tx in history:
        timestamp = parse_timestamp(tx.get("timestamp"))
        if timestamp is None or timestamp > target:
            continue

        difference = (target - timestamp).total_seconds()

        if best_difference is None or difference < best_difference:
            best = tx
            best_difference = difference

    return best


def calculate_price_change(history, current_price, current_time, minutes):
    previous = find_price_before(
        history, current_time, minutes
    )

    if previous is None:
        return None

    old_price = float(previous.get("price", 0))

    if old_price <= 0:
        return None

    return ((current_price - old_price) / old_price) * 100.0


def analyze_token(token_data):
    history = token_data.get("transactions", [])

    if not history:
        return None

    history.sort(key=lambda x: x.get("timestamp", ""))

    latest = history[-1]
    current_price = float(latest.get("price", 0))

    if current_price <= 0:
        return None

    current_time = now_utc()

    window_15m = calculate_window(history, current_time, 15)
    window_30m = calculate_window(history, current_time, 30)
    window_1h = calculate_window(history, current_time, 60)
    window_4h = calculate_window(history, current_time, 240)

    change_15m = calculate_price_change(
        history, current_price, current_time, 15
    )
    change_30m = calculate_price_change(
        history, current_price, current_time, 30
    )
    change_1h = calculate_price_change(
        history, current_price, current_time, 60
    )
    change_4h = calculate_price_change(
        history, current_price, current_time, 240
    )

    # Compare current 15m volume with the preceding 15m.
    previous_15m = get_transactions_in_window(
        history,
        current_time - timedelta(minutes=15),
        15,
    )

    previous_15m_volume = sum(
        float(tx.get("volume", 0))
        for tx in previous_15m
    )

    current_15m_volume = window_15m["total_volume"]

    if previous_15m_volume > 0:
        volume_change = (
            (current_15m_volume - previous_15m_volume)
            / previous_15m_volume
        ) * 100.0
    else:
        volume_change = None

    return {
        "price_in_sda": current_price,
        "last_transaction": latest.get("timestamp"),
        "active": window_1h["total_volume"] >= MIN_RECENT_VOLUME_SDA,
        "momentum": {
            "15m_pct": change_15m,
            "30m_pct": change_30m,
            "1h_pct": change_1h,
            "4h_pct": change_4h,
        },
        "flow": {
            "15m": window_15m,
            "30m": window_30m,
            "1h": window_1h,
            "4h": window_4h,
        },
        "volume_acceleration_15m_pct": volume_change,
        "history_points": len(history),
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")

    if not token or not chat_id:
        print("Telegram credentials not available.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )

    if response.status_code != 200:
        print(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )


# ============================================================
# MAIN
# ============================================================

try:
    print("📈 MARKET SCANNER START")

    transactions = fetch_transactions()
    print(f"Loaded transactions: {len(transactions)}")

    market_data = load_market_data()
    tokens = market_data.setdefault("tokens", {})

    new_transactions = 0

    for raw_tx in transactions:
        tx = normalize_transaction(raw_tx)

        if tx is None:
            continue

        token_address = raw_tx.get("token_address")
        if not token_address:
            continue

        if token_address not in tokens:
            tokens[token_address] = {"transactions": []}

        if add_transaction(tokens[token_address], tx):
            new_transactions += 1

    analyzed_tokens = {}

    for token_address, token_data in tokens.items():
        analysis = analyze_token(token_data)

        if analysis is not None:
            analyzed_tokens[token_address] = analysis

    # Trim stored history after analysis.
    for token_data in tokens.values():
        history = token_data.get("transactions", [])
        history.sort(key=lambda x: x.get("timestamp", ""))

        if len(history) > MAX_HISTORY_POINTS:
            token_data["transactions"] = history[-MAX_HISTORY_POINTS:]

    market_data["updated_at"] = now_utc().isoformat()
    market_data["tokens"] = tokens

    save_market_data(market_data)

    active_tokens = sum(
        1
        for analysis in analyzed_tokens.values()
        if analysis.get("active")
    )

    debug_message = (
        "📈 MARKET DEBUG\n\n"
        f"Loaded transactions: {len(transactions)}\n"
        f"New transactions: {new_transactions}\n"
        f"Tracked tokens: {len(tokens)}\n"
        f"Analyzed tokens: {len(analyzed_tokens)}\n"
        f"Active tokens: {active_tokens}"
    )

    print(debug_message)
    send_telegram(debug_message)

    candidates = []

    for address, analysis in analyzed_tokens.items():
        one_hour = analysis["momentum"].get("1h_pct")

        if one_hour is None:
            continue

        candidates.append({
            "address": address,
            "analysis": analysis,
            "momentum": one_hour,
        })

    candidates.sort(
        key=lambda x: x["momentum"],
        reverse=True,
    )

    if candidates:
        message = "📈 MARKET MOMENTUM\n\n"

        for item in candidates[:10]:
            address = item["address"]
            analysis = item["analysis"]
            momentum = analysis["momentum"]
            flow = analysis["flow"]["1h"]

            message += (
                f"{address[:12]}...\n"
                f"Price: {analysis['price_in_sda']:.10f} SDA\n"
                f"15m: {momentum['15m_pct']:.2f}%\n"
                f"30m: {momentum['30m_pct']:.2f}%\n"
                f"1h: {momentum['1h_pct']:.2f}%\n"
            )

            if momentum["4h_pct"] is not None:
                message += f"4h: {momentum['4h_pct']:.2f}%\n"

            message += (
                f"1h Buy: {flow['buy_volume']:.0f} SDA\n"
                f"1h Sell: {flow['sell_volume']:.0f} SDA\n"
                f"1h Net: {flow['net_flow']:.0f} SDA\n"
                f"1h Trades: "
                f"{flow['buy_count']}/{flow['sell_count']}\n\n"
            )

        send_telegram(message[:4000])

except Exception:
    error_text = traceback.format_exc()

    print(error_text)

    send_telegram(
        "❌ MARKET SCANNER ERROR\n\n"
        f"{error_text[:3500]}"
    )
