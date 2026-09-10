#!/usr/bin/env python3
"""Write a compact dashboard-safe market analysis snapshot.

The full market_data.json keeps transaction history and can become large enough
for remote dashboard fetches to fail. This file contains only the latest
per-token analysis needed by Telegram scoring and technical displays.
"""
import json
from pathlib import Path

src = Path("market_data.json")
dst = Path("market_analysis.json")

try:
    data = json.loads(src.read_text(encoding="utf-8"))
    tokens = data.get("tokens", {}) if isinstance(data, dict) else {}
    compact = {}
    for address, token_data in tokens.items():
        if not isinstance(token_data, dict):
            continue
        analysis = token_data.get("analysis")
        if isinstance(analysis, dict):
            compact[str(address).lower()] = analysis
    out = {"updated_at": data.get("updated_at") if isinstance(data, dict) else None, "tokens": compact}
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(dst)
    print(f"Market analysis export: {len(compact)} tokens")
except Exception as exc:
    print(f"Market analysis export error: {exc}")
    raise
