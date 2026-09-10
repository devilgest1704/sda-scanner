#!/usr/bin/env bash
set -euo pipefail

# Canonical scanner runner: scan -> TA -> metadata/liquidity -> main -> paper stats.
# No source-code rewriting at runtime. Telegram dashboard is intentionally not sent here;
# the hourly workflow owns reporting.

python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py technical_analysis.py telegram_dashboard.py
python whale_scanner.py
python market_scanner.py
python technical_analysis.py
python token_metadata.py
python liquidity_scanner.py

# V19 paper exit tuning is applied safely in-process, without rewriting engine.py.
# BUY logic remains unchanged in main.py.
TELEGRAM_TOKEN="" python - <<'PY'
import engine
import main as scanner_main

engine.TELEGRAM_TOKEN = ""
engine.SL_PCT = 0.05

# Keep the existing engine auto-exit confirmation model, but make its thresholds
# stricter and add a hard emergency protection path. No file/source mutation.
def auto_exit_v19(p, tokens, ws):
    state = engine._load_auto()
    events = []

    for a in list(p.get("positions", {})):
        td = tokens.get(a, {}) or {}
        an = td.get("analysis", td) if isinstance(td, dict) else {}
        cur = engine.num(an.get("price_in_sda"))
        pos = p["positions"].get(a)
        if not pos or cur <= 0:
            continue

        s = engine.score(a, an, ws)
        c = engine.num(s.get("confidence"))
        m1 = engine.num(s.get("m1h"))
        f1 = engine.num(s.get("net_1h"))
        entry = engine.num(pos.get("entry_price"))
        roi = (cur - entry) / entry * 100 if entry else 0.0

        old = state.get(a, {}) if isinstance(state.get(a), dict) else {}
        neg = int(engine.num(old.get("neg")))
        weak = int(engine.num(old.get("weak")))

        emergency = roi <= -15 and c < 35 and m1 < 0 and f1 < 0
        negative = c < 30 and m1 < -1 and f1 < 0 and roi < -3
        weakening = roi > 0 and c < 40 and (m1 < 0 or f1 < 0)

        neg = min(5, neg + 1) if negative else 0
        weak = min(5, weak + 1) if weakening else 0
        state[a] = {"neg": neg, "weak": weak}

        if emergency:
            z = engine.close(p, a, cur, "EMERGENCY SELL")
            if z:
                events.append(
                    f"🚨 EMERGENCY SELL {z['label']} | Profit: {z['closed_profit_sda']:+.2f} SDA | "
                    f"Score: {c:.0f}/100 | ROI: {roi:+.2f}% | Mode: PAPER AUTO"
                )
        elif weak >= 2 and not pos.get("tp1_hit"):
            z = engine.close(p, a, cur, "AUTO PARTIAL SELL", 0.5)
            if z:
                events.append(
                    f"🟠 AUTO PARTIAL SELL {z['label']} | Price: {engine.price(cur)} SDA | "
                    f"Profit: {z['closed_profit_sda']:+.2f} SDA | Score: {c:.0f}/100"
                )
        elif neg >= 3:
            z = engine.close(p, a, cur, "AUTO SELL / EXIT")
            if z:
                events.append(
                    f"🔴 AUTO SELL / EXIT {z['label']} | Price: {engine.price(cur)} SDA | "
                    f"Profit: {z['closed_profit_sda']:+.2f} SDA | Score: {c:.0f}/100"
                )

    engine._save_auto(state)
    return events

engine._auto_exit = auto_exit_v19
scanner_main.engine._auto_exit = auto_exit_v19
scanner_main.engine.SL_PCT = 0.05
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
