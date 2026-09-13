"""Live trade-path instrumentation for V24.

This module is deliberately strategy-neutral: it only records what happened
while a paper position was open. It does not change BUY, SL, TP, or exits.
"""
from datetime import datetime, timezone

THRESHOLDS = (5.0, 10.0, 20.0, 30.0)


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize(position):
    """Initialize V24 path fields on a newly created paper position."""
    if not isinstance(position, dict):
        return position
    entry = _num(position.get("entry_price"))
    if entry <= 0:
        return position
    position.setdefault("v24_entry_price", entry)
    position.setdefault("v24_entry_at", position.get("opened_at") or _now())
    position.setdefault("v24_peak_price", entry)
    position.setdefault("v24_trough_price", entry)
    position.setdefault("v24_mfe_pct", 0.0)
    position.setdefault("v24_mae_pct", 0.0)
    position.setdefault("v24_threshold_times", {})
    return position


def update_position(position, current_price, timestamp=None):
    """Update peak/trough, MFE/MAE and first threshold-hit timestamps."""
    if not isinstance(position, dict):
        return False
    entry = _num(position.get("v24_entry_price") or position.get("entry_price"))
    current = _num(current_price)
    if entry <= 0 or current <= 0:
        return False
    initialize(position)
    ts = timestamp or _now()
    peak = max(_num(position.get("v24_peak_price"), entry), current)
    trough = min(_num(position.get("v24_trough_price"), entry), current)
    mfe = (peak / entry - 1.0) * 100.0
    mae = (trough / entry - 1.0) * 100.0
    changed = (
        peak != _num(position.get("v24_peak_price"), entry)
        or trough != _num(position.get("v24_trough_price"), entry)
        or mfe != _num(position.get("v24_mfe_pct"), 0.0)
        or mae != _num(position.get("v24_mae_pct"), 0.0)
    )
    position["v24_peak_price"] = peak
    position["v24_trough_price"] = trough
    position["v24_mfe_pct"] = round(mfe, 6)
    position["v24_mae_pct"] = round(mae, 6)
    hits = position.get("v24_threshold_times")
    if not isinstance(hits, dict):
        hits = {}
        position["v24_threshold_times"] = hits
    for threshold in THRESHOLDS:
        key = str(int(threshold))
        if threshold <= mfe and key not in hits:
            hits[key] = ts
            changed = True
    position["v24_last_price"] = current
    position["v24_last_seen_at"] = ts
    return changed


def update_open_positions(paper_engine):
    """Refresh all currently open positions from the latest market snapshot."""
    try:
        p = paper_engine.load(paper_engine.POSITIONS_FILE, {"positions": {}, "closed_trades": []})
        positions = p.get("positions", {}) if isinstance(p, dict) else {}
        md = paper_engine.load(paper_engine.MARKET_FILE, {"tokens": {}})
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        changed = False
        ts = _now()
        for address, position in positions.items():
            if not isinstance(position, dict):
                continue
            token = tokens.get(str(address).lower(), {})
            if isinstance(token, dict) and isinstance(token.get("analysis"), dict):
                token = token["analysis"]
            if not isinstance(token, dict):
                continue
            current = _num(token.get("price_in_sda"))
            if current <= 0:
                continue
            if update_position(position, current, ts):
                changed = True
        if changed:
            paper_engine.save(paper_engine.POSITIONS_FILE, p)
    except Exception:
        # Instrumentation must never be able to stop the scanner.
        return False
    return changed
