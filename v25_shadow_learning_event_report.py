"""Event-level false-negative analysis for V25 shadow learning.

Observational only. Repeated snapshots of the same token are grouped into
contiguous scan events so one pump is not counted as many independent misses.
No V25 gate or MAX-WIN decision is changed.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List

STATE_FILE = "v25_shadow_learning_v2.json"
REPORT_FILE = "v25_shadow_learning_event_report.json"
HORIZONS = (5, 10, 20)
THRESHOLDS = (0.0, 3.0, 5.0, 10.0)
MAX_SCAN_GAP = 2


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def _scan(row: Dict[str, Any]) -> int | None:
    v = row.get("scan")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _outcome(row: Dict[str, Any], horizon: int) -> float | None:
    out = (row.get("outcomes") or {}).get(str(horizon))
    if not isinstance(out, dict):
        return None
    return _num(out.get("net_return_pct"))


def _identity(row: Dict[str, Any]) -> str:
    for key in ("address", "token", "symbol", "name"):
        value = row.get(key)
        if value:
            return str(value)
    return "UNKNOWN"


def _reason_group(reason: str) -> str:
    s = str(reason).upper()
    if s.startswith("PUMP SCORE"):
        return "PUMP_SCORE"
    if s.startswith("VOLUME"):
        return "VOLUME"
    if s.startswith("TRADES"):
        return "TRADES"
    for group in ("M1H", "M15", "M4H"):
        if s.startswith(group):
            return group
    if s.startswith("FLOW"):
        return "FLOW"
    if "TECHNICAL BEAR" in s:
        return "TECHNICAL_BEAR"
    if "PUMP ACCELERATION" in s or "PUMP ACCEL" in s or "PRESSURE" in s:
        return "PUMP_ACCEL_PRESSURE"
    if "LEARNER EV" in s:
        return "LEARNER_EV"
    if "LEARNER DOWNSIDE" in s:
        return "LEARNER_DOWNSIDE"
    return "UNKNOWN"


def _groups(row: Dict[str, Any]) -> List[str]:
    return sorted({_reason_group(r) for r in (row.get("rejection_reasons") or [])})


def _features(row: Dict[str, Any]) -> Dict[str, float]:
    f = row.get("features") or {}
    keys = ("pump_score", "score", "volume", "trades", "m1h", "m15", "m4h", "flow", "accel", "volume_ratio", "trade_ratio", "p5", "p10", "p20", "p30")
    return {k: _num(f.get(k)) for k in keys if _num(f.get(k)) is not None}


def _make_events(rows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row, dict) and not row.get("allowed"):
            buckets[_identity(row)].append(row)
    events: List[List[Dict[str, Any]]] = []
    for token_rows in buckets.values():
        token_rows.sort(key=lambda r: (_scan(r) if _scan(r) is not None else 10**12))
        current: List[Dict[str, Any]] = []
        last_scan: int | None = None
        for row in token_rows:
            scan = _scan(row)
            if current and (scan is None or last_scan is None or scan - last_scan > MAX_SCAN_GAP):
                events.append(current)
                current = []
            current.append(row)
            last_scan = scan
        if current:
            events.append(current)
    events.sort(key=lambda e: (_scan(e[0]) if _scan(e[0]) is not None else 10**12, _identity(e[0])))
    return events


def _event_record(event: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = event[0]
    all_groups = sorted({g for row in event for g in _groups(row)})
    best = {}
    for horizon in HORIZONS:
        vals = [v for v in (_outcome(row, horizon) for row in event) if v is not None]
        if vals:
            best[str(horizon)] = round(max(vals), 3)
    return {
        "identity": _identity(first),
        "first_scan": _scan(first),
        "last_scan": _scan(event[-1]),
        "snapshot_count": len(event),
        "best_net_return_pct": best,
        "first_rejection_groups": _groups(first),
        "event_rejection_groups": all_groups,
        "first_features": _features(first),
    }


def _stats(events: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    completed = []
    for event in events:
        first = event[0]
        value = _outcome(first, 10)
        if value is None:
            values = [_outcome(r, 10) for r in event]
            values = [v for v in values if v is not None]
            value = max(values) if values else None
        if value is not None:
            completed.append((value, event))
    result: Dict[str, Any] = {"event_count": len(events), "completed_10h": len(completed)}
    for threshold in THRESHOLDS:
        selected = [(v, e) for v, e in completed if v > threshold]
        result[f"gt_{str(threshold).replace('.', '_')}_pct"] = {
            "count": len(selected),
            "avg_net_return_pct": round(statistics.mean(v for v, _ in selected), 3) if selected else None,
            "median_net_return_pct": round(statistics.median(v for v, _ in selected), 3) if selected else None,
            "best_net_return_pct": round(max(v for v, _ in selected), 3) if selected else None,
        }
    return result


def _group_stats(events: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    names = sorted({g for e in events for r in e for g in _groups(r)})
    for name in names:
        matched = [e for e in events if name in {g for r in e for g in _groups(r)}]
        vals = []
        for e in matched:
            candidates = [_outcome(r, 10) for r in e]
            candidates = [v for v in candidates if v is not None]
            if candidates:
                vals.append(max(candidates))
        wins = [v for v in vals if v > 0]
        out[name] = {
            "events": len(matched),
            "completed_10h": len(vals),
            "wins_gt_0_pct": len(wins),
            "win_rate_pct": round(100 * len(wins) / len(vals), 2) if vals else None,
            "avg_best_10h_net_return_pct": round(statistics.mean(vals), 3) if vals else None,
        }
    return out


def build_report(data: Dict[str, Any]) -> Dict[str, Any]:
    events = _make_events(data.get("observations", []))
    records = [_event_record(e) for e in events]
    records_10 = []
    for rec in records:
        v = rec.get("best_net_return_pct", {}).get("10")
        if isinstance(v, (int, float)) and v > 10:
            records_10.append(rec)
    records_10.sort(key=lambda r: r["best_net_return_pct"]["10"], reverse=True)
    return {
        "generated_at": time.time(),
        "source_observations": len(data.get("observations", [])),
        "deduplication": {"key": "address/token/symbol/name", "max_scan_gap": MAX_SCAN_GAP},
        "stats": _stats(events),
        "rejection_group_stats": _group_stats(events),
        "top_unique_events_gt_10_pct": records_10[:10],
        "note": "Observational only. Event deduplication does not alter V25 gate decisions or MAX-WIN.",
    }


def write_report() -> Dict[str, Any]:
    report = build_report(_load(STATE_FILE))
    tmp = REPORT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REPORT_FILE)
    return report


if __name__ == "__main__":
    write_report()
