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

# Main scanner remains responsible for state generation. Direct Telegram output is suppressed.
TELEGRAM_TOKEN="" python main.py
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
