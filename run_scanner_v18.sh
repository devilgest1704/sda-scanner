#!/usr/bin/env bash
set -euo pipefail

python - <<'PY'
from pathlib import Path
import re

# Market activity: >=250 SDA in last hour; trade count is NOT a gate.
p = Path('market_scanner.py')
s = p.read_text(encoding='utf-8')
old = '''        "active": (\n            window_1h["total_volume"] >= MIN_RECENT_VOLUME_SDA\n            and window_1h["transactions"] >= MIN_RECENT_TRADES\n        ),'''
new = '''        # Active means at least 250 SDA traded during the last hour.\n        # Trade count is intentionally NOT part of the activity filter.\n        "active": window_1h["total_volume"] >= MIN_RECENT_VOLUME_SDA,'''
if old in s:
    s = s.replace(old, new, 1)
p.write_text(s, encoding='utf-8')

# Canonical REAL PORTFOLIO renderer. Replace the whole function so workflow
# cannot resurrect an older visual variant.
p = Path('main.py')
s = p.read_text(encoding='utf-8')
start = s.find('def portfolio_message_v16(portfolio, recommendations):')
end = s.find('\ndef wallet_message_v16(w):', start)
if start < 0 or end < 0:
    raise SystemExit('portfolio_message_v16 block not found')

fn = '''def portfolio_message_v16(portfolio, recommendations):
    meta = engine.load(engine.META_FILE, {})
    cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    lines = ["📊 REAL PORTFOLIO", "", "Current token positions — SDA accumulation", "────────────────────────"]

    for token, pf in sorted(cur.items(), key=lambda x: (x[1].get("symbol") or x[0]).lower()):
        m = meta.get(token, {}) or {}
        symbol = pf.get("symbol") or m.get("symbol") or token[:10] + "..."
        name = m.get("name") or symbol
        val = pf.get("value_sda")
        pnl = pf.get("unrealized_pnl_sda")
        pct = pf.get("unrealized_pnl_pct")
        value_text = f"{_n(val):.2f} SDA" if val is not None else "UNKNOWN"
        if pnl is None or pct is None:
            pnl_text = "⚪ P/L UNKNOWN"
        else:
            pnl_icon = "🟢" if _n(pnl) >= 0 else "🔴"
            pnl_text = f"{pnl_icon} P/L {_n(pnl):+.2f} SDA ({_n(pct):+.2f}%)"
        lines += [
            f"🪙 {symbol} — {name}",
            f"   {_fmt_amount(pf.get('amount'))} {symbol}  •  {value_text}",
            f"   {pnl_text}",
            "",
        ]

    op = portfolio.get("open_unrealized_pnl_sda")
    rp = portfolio.get("realized_pnl_sda")
    tp = portfolio.get("known_total_pnl_sda")
    open_cost = _n(portfolio.get("open_cost_sda"))
    op_pct = _n(op) / open_cost * 100 if op is not None and open_cost else None

    lines += ["────────────────────────"]
    if op is None:
        lines.append("⚪ Current open P/L UNKNOWN")
    else:
        icon = "🟢" if _n(op) >= 0 else "🔴"
        suffix = f" ({op_pct:+.2f}%)" if op_pct is not None else ""
        lines.append(f"{icon} Current open P/L {_n(op):+.2f} SDA{suffix}")

    r_icon = "🟢" if _n(rp) >= 0 else "🔴"
    t_icon = "🟢" if _n(tp) >= 0 else "🔴"
    lines.append(f"{r_icon} Historical matched P/L {_n(rp):+.2f} SDA")
    lines.append(f"{t_icon} Known total P/L {_n(tp):+.2f} SDA")
    lines += [
        f"Matched sells: {int(_n(portfolio.get('matched_sell_count')))}  •  Excluded unmatched: {int(_n(portfolio.get('excluded_unmatched_sell_count')))}",
        "",
        "🧭 POSITION ACTION",
    ]

    def _strength(score):
        s = _n(score)
        if s < 20:
            return "🔴 VERY WEAK"
        if s < 40:
            return "🟠 WEAK"
        if s < 60:
            return "🟡 NEUTRAL"
        if s < 75:
            return "🟢 GOOD"
        return "🟢 STRONG"

    for r in recommendations:
        action = r.get("action")
        icon = "🔴" if action == "SELL / EXIT" else ("🟠" if action == "PARTIAL SELL" else ("🟢" if action == "HOLD / TRAIL" else "🟡"))
        pnl = r.get("pnl_sda")
        pnl_text = f"{_n(pnl):+.2f} SDA" if pnl is not None else "UNKNOWN"
        score = int(_n(r.get("score")))
        lines.append(f"{icon} {r['symbol']}: {action}  •  P/L {pnl_text}  •  score {score}/100 — {_strength(score)}")

    lines += [
        "",
        "ℹ️ Score: 0–19 VERY WEAK • 20–39 WEAK • 40–59 NEUTRAL • 60–74 GOOD • 75–100 STRONG",
        "ℹ️ Token names from Blockscout metadata.",
        "ℹ️ Invalid/inconsistent prices are UNKNOWN — never a fake loss.",
        "🔒 Wallet is READ-ONLY.",
    ]
    return "\\n".join(lines)
'''
s = s[:start] + fn + s[end:]
p.write_text(s, encoding='utf-8')

# V18 paper configuration.
p = Path('main.py')
s = p.read_text(encoding='utf-8')
s = s.replace('engine.MAX_NEW_BUYS_PER_RUN = 1', 'engine.MAX_NEW_BUYS_PER_RUN = 5\nengine.MAX_OPEN_POSITIONS = 10', 1)
p.write_text(s, encoding='utf-8')

# Liquidity scanner compatibility fixes.
p = Path('liquidity_scanner.py')
s = p.read_text(encoding='utf-8')
s = s.replace('"token_decimals": token_out["decimals"] if token_out else None,', '"token_decimals": token_out.get("decimals") if token_out else None,', 1)
s = s.replace('bool(discovery.get("v25_50_sda_quote_probe", {}).get("ok"))', 'bool((discovery.get("v25_50_sda_quote_probe") or {}).get("ok"))', 1)
marker = '    transfers = receipt_erc20_transfers(h)\n    if not transfers:\n        transfers = transfers_for_tx(h)\n'
enrichment = '''    transfers = receipt_erc20_transfers(h)\n    if not transfers:\n        transfers = transfers_for_tx(h)\n    metadata = load_json("token_metadata.json", {})\n    if isinstance(metadata, dict):\n        for t in transfers:\n            a = addr(t.get("token_address"))\n            meta = metadata.get(a) or metadata.get(a.lower())\n            if isinstance(meta, dict) and meta.get("decimals") is not None and t.get("raw_value") is not None:\n                try:\n                    d = int(meta.get("decimals"))\n                    if 0 <= d <= 36:\n                        t["decimals"] = d\n                        t["value"] = float(Decimal(t["raw_value"]) / (Decimal(10) ** d))\n                except Exception:\n                    pass\n'''
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
