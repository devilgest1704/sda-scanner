#!/usr/bin/env bash
set -euo pipefail

python - <<'PY'
from pathlib import Path

# Market activity: >=250 SDA in last hour; trade count is NOT a gate.
p = Path('market_scanner.py')
s = p.read_text(encoding='utf-8')
old = '''        "active": (
            window_1h["total_volume"] >= MIN_RECENT_VOLUME_SDA
            and window_1h["transactions"] >= MIN_RECENT_TRADES
        ),'''
new = '''        # Active means at least 250 SDA traded during the last hour.
        # Trade count is intentionally NOT part of the activity filter.
        "active": window_1h["total_volume"] >= MIN_RECENT_VOLUME_SDA,'''
if old in s:
    s = s.replace(old, new, 1)
p.write_text(s, encoding='utf-8')

# Liquidity scanner compatibility fixes.
p = Path('liquidity_scanner.py')
s = p.read_text(encoding='utf-8')
s = s.replace('"token_decimals": token_out["decimals"] if token_out else None,', '"token_decimals": token_out.get("decimals") if token_out else None,', 1)
s = s.replace('bool(discovery.get("v25_50_sda_quote_probe", {}).get("ok"))', 'bool((discovery.get("v25_50_sda_quote_probe") or {}).get("ok"))', 1)
marker = '    transfers = receipt_erc20_transfers(h)\n    if not transfers:\n        transfers = transfers_for_tx(h)\n'
enrichment = '''    transfers = receipt_erc20_transfers(h)
    if not transfers:
        transfers = transfers_for_tx(h)
    metadata = load_json("token_metadata.json", {})
    if isinstance(metadata, dict):
        for t in transfers:
            a = addr(t.get("token_address"))
            meta = metadata.get(a) or metadata.get(a.lower())
            if isinstance(meta, dict) and meta.get("decimals") is not None and t.get("raw_value") is not None:
                try:
                    d = int(meta.get("decimals"))
                    if 0 <= d <= 36:
                        t["decimals"] = d
                        t["value"] = float(Decimal(t["raw_value"]) / (Decimal(10) ** d))
                except Exception:
                    pass
'''
if marker in s and 'metadata = load_json("token_metadata.json", {})' not in s:
    s = s.replace(marker, enrichment, 1)
p.write_text(s, encoding='utf-8')
PY

python -m py_compile market_scanner.py liquidity_scanner.py main.py engine.py
python whale_scanner.py
python market_scanner.py
python token_metadata.py
python liquidity_scanner.py
python main.py
python paper_stats.py

python - <<'PY'
import json
from pathlib import Path
files = '''state.json whale_state.json whale_data.json whale_history.json portfolio_data.json market_data.json token_metadata.json liquidity_data.json paper_stats.json sidra_swap_discovery.json pending_signals.json positions.json wallet_data.json decision_state_v15.json paper_auto_state.json'''.split()
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
