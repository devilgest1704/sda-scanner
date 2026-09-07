import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SUPABASE_URL = "https://uhrsigapvhlpudafxqfg.supabase.co"
SUPABASE_KEY = "sb_publishable_fL6m94CTRdZESg1licW9Qw_BuLIkm1Z"

DATA_URL = f"{SUPABASE_URL}/rest/v1/rpc/get_token_stats_24h"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Content-Profile": "public"
}


def load_signal(address):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/scanner_signals",
        params={
            "token_address": f"eq.{address}",
            "select": "signal,score"
        },
        headers=HEADERS,
        timeout=30
    )

    data = r.json()

    if not data:
        return None

    return data[0]


def save_signal(address, signal, score):

    requests.post(
        f"{SUPABASE_URL}/rest/v1/scanner_signals",
        headers={
            **HEADERS,
            "Prefer": "resolution=merge-duplicates"
        },
        json={
            "token_address": address,
            "signal": signal,
            "score": score
        },
        timeout=30
    )


try:

    response = requests.post(
        DATA_URL,
        headers=HEADERS,
        json={},
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    alerts = []
    checked = 0

    for token in data:

        buy = float(token.get("buy_vol_sda", 0))
        sell = float(token.get("sell_vol_sda", 0))
        total = float(token.get("total_vol_sda", 0))

        buy_count = int(token.get("buy_count", 0))
        sell_count = int(token.get("sell_count", 0))

        if total < 1000:
            continue

        checked += 1

        strength = buy / max(sell, 1)

        trade_ratio = (
            buy_count /
            max(sell_count, 1)
        )

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

        previous = load_signal(token["token_address"])

        changed = (
            previous is None
            or previous["signal"] != signal
        )

        if changed:

            alerts.append(
                f"{signal}\n"
                f"{token['token_address'][:12]}...\n"
                f"Score: {score:.2f}\n"
                f"Strength: {strength:.2f}\n"
                f"Volume: {total:.0f} SDA"
            )

        save_signal(
            token["token_address"],
            signal,
            score
        )

    if alerts:

        message = (
            "🚨 SDA SIGNAL CHANGES\n\n"
            f"Tokens checked: {checked}\n"
            f"Signal changes: {len(alerts)}\n\n"
            + "\n\n".join(alerts[:10])
        )

    else:

        message = (
            "✅ SDA SCANNER\n\n"
            f"Tokens checked: {checked}\n"
            f"Signal changes: 0\n\n"
            "No new signals"
        )

except Exception as e:

    message = (
        "❌ SCANNER ERROR\n\n"
        f"{str(e)}"
    )

requests.post(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    json={
        "chat_id": CHAT_ID,
        "text": message
    },
    timeout=30
)

print("Done")
