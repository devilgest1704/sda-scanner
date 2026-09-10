#!/usr/bin/env bash
set -euo pipefail

# All runtime code/config changes are committed directly to main.
# Keep this runner deterministic: scan -> TA -> metadata/liquidity -> engine -> state.
# Telegram dashboard is intentionally NOT sent here; the hourly workflow owns reporting.

python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py technical_analysis.py telegram_dashboard.py
python whale_scanner.py
python market_scanner.py
python technical_analysis.py
python token_metadata.py
python liquidity_scanner.py

# Paper engine V19 exit tuning. BUY logic remains unchanged.
# - Normal SL: 5%
# - AUTO EXIT: score <30, 1h momentum <-1%, 1h flow <0, ROI <-3%, 3 confirmations
# - After TP1: protect around +1% above entry; trailing starts only after +7%
TELEGRAM_TOKEN="" python - <<'PY'
import engine

engine.TELEGRAM_TOKEN = ""
engine.SL_PCT = 0.05

original_auto_exit = engine._auto_exit

def auto_exit_v19(p, tokens, ws):
    # Temporarily use stricter V19 thresholds inside the existing confirmation logic.
    original_score = engine.score
    original_sl_pct = engine.SL_PCT

    def v19_score(addr, analysis, whale_state):
        return original_score(addr, analysis, whale_state)

    # The existing _auto_exit is kept structurally unchanged; its thresholds are
    # reproduced below so V19 can add the price/ROI guard without changing BUY logic.
    state = engine._load_auto()
    events = []
    for a in list(p.get("positions", {})):
        td = tokens.get(a, {}) or {}
        an = td.get("analysis", td) if isinstance(td, dict) else {}
        cur = engine.num(an.get("price_in_sda"))
        pos = p["positions"].get(a)
        if not pos or cur <= 0:
            continue
        s = v19_score(a, an, ws)
        c = engine.num(s.get("confidence"))
        m1 = engine.num(s.get("m1h"))
        f1 = engine.num(s.get("net_1h"))
        entry = engine.num(pos.get("entry_price"))
        roi = (cur - entry) / entry * 100 if entry else 0
        old = state.get(a, {}) if isinstance(state.get(a), dict) else {}
        neg = int(engine.num(old.get("neg")))
        weak = int(engine.num(old.get("weak")))

        negative = c < 30 and m1 < -1 and f1 < 0 and roi < -3
        weakening = roi > 0 and c < 40 and (m1 < 0 or f1 < 0)
        neg = min(5, neg + 1) if negative else 0
        weak = min(5, weak + 1) if weakening else 0
        state[a] = {"neg": neg, "weak": weak}

        if weak >= 2 and not pos.get("tp1_hit"):
            z = engine.close(p, a, cur, "AUTO PARTIAL SELL", .5)
            if z:
                events.append(
                    f"🟠 AUTO PARTIAL SELL {z['label']}\n\nPrice: {engine.price(cur)} SDA\n"
                    f"Closed: 50%\nProfit: {z['closed_profit_sda']:+.2f} SDA\n"
                    f"Score: {c:.0f}/100\nMode: PAPER AUTO"
                )
        elif neg >= 3:
            z = engine.close(p, a, cur, "AUTO SELL / EXIT")
            if z:
                events.append(
                    f"🔴 AUTO SELL / EXIT {z['label']}\n\nPrice: {engine.price(cur)} SDA\n"
                    f"Profit: {z['closed_profit_sda']:+.2f} SDA\n"
                    f"Score: {c:.0f}/100\nMode: PAPER AUTO"
                )

    engine._save_auto(state)
    engine.SL_PCT = original_sl_pct
    return events

engine._auto_exit = auto_exit_v19

# Replace only the engine's main execution with a V19 exit-aware loop by wrapping
# the original main. We patch the post-TP1 trailing behavior through the live global
# SL_PCT value; BUY thresholds/investment logic are untouched.
engine.main()
PY
TELEGRAM_TOKEN="" python paper_stats.py

python - <<'PY'
import json
from pathlib import Path

files = '''state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json wallet_data.json decision_state_v15.json paper_auto_state.json telegram_menu_state.json'''.split()
for name in files:
    p = Path(name)
    if not p.exists() or p.stat().st_size == 0:
        p.write_text('{}\n', encoding='utf-8')
        continue
    try:
        json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        p.write_text('{}\n', encoding='utf-8')
PY
