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
# - Emergency exit: ROI <=-15%, score <35, negative 1h momentum and flow; no confirmation wait
# - After TP1: protect at about +1%; 5% trailing only after +7% from entry
TELEGRAM_TOKEN="" python - <<'PY'
from pathlib import Path

source = Path("engine.py").read_text(encoding="utf-8")

source = source.replace(
    'TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.04; FEE_RATE=.01; SLIPPAGE_RATE=.001',
    'TP1_PCT=.05; TP2_PCT=.10; SL_PCT=.05; FEE_RATE=.01; SLIPPAGE_RATE=.001',
    1,
)

source = source.replace(
    'negative=c<35 and m1<0 and f1<0;weakening=roi>0 and c<40 and (m1<0 or f1<0)',
    'negative=c<30 and m1<-1 and f1<0 and roi<-3;weakening=roi>0 and c<40 and (m1<0 or f1<0)',
    1,
)

source = source.replace(
    'if pos.get("tp1_hit"):\n            breakeven=e*(1+FEE_RATE)/((1-SLIPPAGE_RATE)*(1-FEE_RATE));pos["sl"]=max(num(pos.get("sl")),breakeven,cur*(1-SL_PCT))',
    'if pos.get("tp1_hit"):\n            protected=e*1.01\n            if cur>=e*1.07:\n                pos["sl"]=max(num(pos.get("sl")),protected,cur*(1-SL_PCT))\n            else:\n                pos["sl"]=max(num(pos.get("sl")),protected)',
    1,
)

# Inject the emergency exit directly into _auto_exit before normal confirmation exits.
source = source.replace(
    'negative=c<35 and m1<0 and f1<0;weakening=roi>0 and c<40 and (m1<0 or f1<0)\n        neg=min(5,neg+1) if negative else 0;weak=min(5,weak+1) if weakening else 0;state[a]={"neg":neg,"weak":weak}\n        if weak>=2',
    'emergency=roi<=-15 and c<35 and m1<0 and f1<0;negative=c<30 and m1<-1 and f1<0 and roi<-3;weakening=roi>0 and c<40 and (m1<0 or f1<0)\n        neg=min(5,neg+1) if negative else 0;weak=min(5,weak+1) if weakening else 0;state[a]={"neg":neg,"weak":weak}\n        if emergency:\n            z=close(p,a,cur,"EMERGENCY SELL")\n            if z:events.append(f"🚨 EMERGENCY SELL {z['label']} | Profit: {z['closed_profit_sda']:+.2f} SDA | Score: {c:.0f}/100 | ROI: {roi:+.2f}%")\n        elif weak>=2',
    1,
)

required = [
    'SL_PCT=.05',
    'negative=c<30 and m1<-1 and f1<0 and roi<-3',
    'protected=e*1.01',
    'if cur>=e*1.07:',
    'emergency=roi<=-15 and c<35 and m1<0 and f1<0',
    'EMERGENCY SELL',
]
for marker in required:
    if marker not in source:
        raise RuntimeError(f"V19 patch marker missing: {marker}")

ns = {"__name__": "__main__", "__file__": "engine.py"}
exec(compile(source, "engine.py [V19 runtime]", "exec"), ns, ns)
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
