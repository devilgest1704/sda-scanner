# Robust rolling whale scanner.
# Fetches recent trades without relying on Supabase column-side filtering,
# then classifies trades locally. This tolerates tx_type/type/side variants.
import json, traceback, os
from datetime import datetime, timezone, timedelta
import requests

DATA = "whale_data.json"
HISTORY = "whale_history.json"
STATE = "whale_state.json"

BASE = "https://uhrsigapvhlpudafxqfg.supabase.co/rest/v1/token_transactions"
URL = (
    BASE
    + "?select=*&order=tx_timestamp.desc&limit=1000"
)
HEADERS = {
    "apikey": "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z",
    "accept-profile": "public",
}

WHALE_MIN_SDA = 5000.0
WINDOWS = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}


def load(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def save(p, d):
    t = p + ".tmp"
    with open(t, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(t, p)


def ts(v):
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def classify_side(row):
    raw = first(row, "tx_type", "trade_type", "side", "type", "action", "direction")
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in {"buy", "b", "purchase", "swap_buy", "token_buy"}:
        return "buy"
    if s in {"sell", "s", "sale", "swap_sell", "token_sell"}:
        return "sell"
    if "buy" in s:
        return "buy"
    if "sell" in s:
        return "sell"
    return None


def volume(row):
    v = first(
        row,
        "volume_in_sda",
        "volume_sda",
        "amount_in_sda",
        "sda_amount",
        "value_sda",
        "trade_volume_sda",
    )
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def token_address(row):
    v = first(row, "token_address", "token", "token_address_hash", "address")
    return str(v or "").lower()


def tx_hash(row):
    return str(first(row, "tx_hash", "transaction_hash", "hash", "id") or "")


def main():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected Supabase response: {type(rows).__name__}")

    old = load(HISTORY, [])
    by = {
        str(x.get("tx_hash")): x
        for x in old
        if isinstance(x, dict) and x.get("tx_hash")
    }

    now_utc = datetime.now(timezone.utc)

    for row in rows:
        if not isinstance(row, dict):
            continue

        h = tx_hash(row)
        t = ts(first(row, "tx_timestamp", "timestamp", "created_at", "block_timestamp"))
        a = token_address(row)
        side = classify_side(row)
        vol = volume(row)

        # Only store actual whale trades; classification is local and robust.
        if h and t and a.startswith("0x") and side in {"buy", "sell"} and vol >= WHALE_MIN_SDA:
            by[h] = {
                "tx_hash": h,
                "token_address": a,
                "tx_timestamp": t.isoformat(),
                "tx_type": side,
                "volume_in_sda": vol,
            }

    cutoff = now_utc - timedelta(minutes=360)
    hist = [
        x for x in by.values()
        if (ts(x.get("tx_timestamp")) or cutoff) >= cutoff
    ]
    hist.sort(key=lambda x: x.get("tx_timestamp", ""), reverse=True)
    save(HISTORY, hist)

    out = {}
    for a in {x["token_address"] for x in hist}:
        windows = {}
        token_rows = [x for x in hist if x["token_address"] == a]

        for name, mins in WINDOWS.items():
            c = now_utc - timedelta(minutes=mins)
            v = [
                x for x in token_rows
                if (ts(x.get("tx_timestamp")) or now_utc) >= c
            ]
            buy_volume = sum(x["volume_in_sda"] for x in v if x["tx_type"] == "buy")
            sell_volume = sum(x["volume_in_sda"] for x in v if x["tx_type"] == "sell")
            buy_count = sum(x["tx_type"] == "buy" for x in v)
            sell_count = sum(x["tx_type"] == "sell" for x in v)

            windows[name] = {
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "net_flow": buy_volume - sell_volume,
                "buy_count": buy_count,
                "sell_count": sell_count,
                "trade_count": len(v),
            }

        out[a] = {
            "windows": windows,
            "whale_buy_volume": windows["1h"]["buy_volume"],
            "whale_sell_volume": windows["1h"]["sell_volume"],
            "whale_buy_count": windows["1h"]["buy_count"],
            "whale_sell_count": windows["1h"]["sell_count"],
            "current_1h_net": windows["1h"]["net_flow"],
            "updated_at": now_utc.isoformat(),
        }

    save(DATA, out)
    save(
        STATE,
        {
            "updated_at": now_utc.isoformat(),
            "history_points": len(hist),
            "tokens": len(out),
            "source_rows": len(rows),
            "whale_min_sda": WHALE_MIN_SDA,
        },
    )

    print(
        f"🐋 WHALE DEBUG\n\n"
        f"Loaded transactions: {len(rows)}\n"
        f"Whale history (6h): {len(hist)}\n"
        f"Tracked whale tokens: {len(out)}\n"
        f"Threshold: {WHALE_MIN_SDA:.0f} SDA"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(traceback.format_exc())
        raise
