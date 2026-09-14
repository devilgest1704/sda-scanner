"""V25 Shadow Learning v2: append-only observation log and horizon outcomes.

Isolated from MAX-WIN. Records WATCH candidates so V25 can learn from missed
entries without changing the live BUY gate.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

STATE_FILE = "v25_shadow_learning_v2.json"
HORIZONS = (5, 10, 20, 30)
MAX_RECORDS = 2000


def _load() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"observations": []}
    except Exception:
        return {"observations": []}


def _save(data: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def record_observation(candidate: Dict[str, Any], *, lane: str, allowed: bool,
                       reasons: List[str], scan: int, price: float) -> None:
    """Append one WATCH/decision observation; duplicates for same scan are ignored."""
    address = str(candidate.get("address") or candidate.get("token") or "").lower()
    if not address or price <= 0:
        return
    data = _load()
    observations = data.setdefault("observations", [])
    key = f"{address}:{scan}"
    if any(str(x.get("key")) == key for x in observations[-100:]):
        return
    observations.append({
        "key": key, "timestamp": time.time(), "scan": scan,
        "address": address, "symbol": candidate.get("symbol"),
        "lane": lane, "allowed": bool(allowed),
        "rejection_reasons": list(reasons or []), "entry_price": float(price),
        "features": {k: candidate.get(k) for k in (
            "pump_score", "score", "volume", "trades", "m1h", "m15", "m4h",
            "flow", "accel", "bear", "p5", "p10", "p20", "p30",
            "expected_mfe", "expected_mae", "ev") if k in candidate},
        "outcomes": {},
    })
    data["observations"] = observations[-MAX_RECORDS:]
    _save(data)


def update_horizons(price_by_address: Dict[str, float], scan: int) -> None:
    """Close observation horizons and store gross/hypothetical post-cost returns."""
    data = _load()
    changed = False
    for row in data.get("observations", []):
        if not isinstance(row, dict):
            continue
        entry_scan = int(row.get("scan") or 0)
        delta = scan - entry_scan
        if delta not in HORIZONS:
            continue
        address = str(row.get("address") or "").lower()
        current = float(price_by_address.get(address) or 0)
        entry = float(row.get("entry_price") or 0)
        if current <= 0 or entry <= 0:
            continue
        ret = (current / entry - 1.0) * 100.0
        # Conservative paper estimate: 1% fee + 0.1% slippage each side.
        net = (current * 0.999 * 0.99) / (entry * 1.01) - 1.0
        out = row.setdefault("outcomes", {})
        if str(delta) not in out:
            out[str(delta)] = {"return_pct": ret, "net_return_pct": net * 100.0}
            changed = True
    if changed:
        _save(data)


def summary() -> Dict[str, Any]:
    data = _load()
    rows = data.get("observations", [])
    result: Dict[str, Any] = {"observations": len(rows), "horizons": {}}
    for h in HORIZONS:
        vals = []
        for row in rows:
            out = (row.get("outcomes") or {}).get(str(h)) if isinstance(row, dict) else None
            if isinstance(out, dict) and isinstance(out.get("net_return_pct"), (int, float)):
                vals.append(float(out["net_return_pct"]))
        if vals:
            wins = sum(v > 0 for v in vals)
            result["horizons"][str(h)] = {
                "completed": len(vals), "wins": wins,
                "win_rate_pct": round(wins / len(vals) * 100, 1),
                "avg_net_return_pct": round(sum(vals) / len(vals), 2),
            }
    return result
