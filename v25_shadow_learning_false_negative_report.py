"""False-negative drill-down report for V25 shadow learning."""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from typing import Any, Dict

STATE_FILE = "v25_shadow_learning_v2.json"
REPORT_FILE = "v25_shadow_learning_false_negative_report.json"
HORIZON = 10
THRESHOLD = 10.0
REVERSAL_SCORE_THRESHOLDS = (30.0, 35.0, 40.0, 45.0, 48.0, 50.0, 55.0, 60.0, 68.0)
MAX_SCAN_GAP = 2


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _outcome(row: Dict[str, Any], horizon: int = HORIZON) -> float | None:
    out = (row.get("outcomes") or {}).get(str(horizon))
    if not isinstance(out, dict):
        return None
    value = out.get("net_return_pct")
    return float(value) if isinstance(value, (int, float)) else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _pick(row: Dict[str, Any]) -> Dict[str, Any]:
    features = row.get("features") or {}
    wanted = (
        "pump_score", "score", "volume", "trades", "m1h", "m15", "m4h",
        "flow", "accel", "volume_ratio", "trade_ratio", "p5", "p10", "p20", "p30",
    )
    return {
        key: round(value, 3)
        for key in wanted
        if (value := _num(features.get(key))) is not None
    }


def _identity_key(row: Dict[str, Any]) -> str:
    return str(row.get("address") or row.get("token") or row.get("symbol") or row.get("name") or "UNKNOWN")


def _identity(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: row.get(key) for key in ("address", "symbol", "token", "name", "scan", "lane", "allowed") if key in row}


def _reversal_shape(row: Dict[str, Any]) -> bool:
    f = row.get("features") or {}
    m1h, flow, trades = _num(f.get("m1h")), _num(f.get("flow")), _num(f.get("trades"))
    m15, m4h = _num(f.get("m15")), _num(f.get("m4h"))
    p5, p10 = _num(f.get("p5")), _num(f.get("p10"))
    return (
        m1h is not None and m1h >= 8.0 and
        flow is not None and flow > 0.0 and
        trades is not None and trades >= 20.0 and
        m15 is not None and -8.0 <= m15 < 0.0 and
        m4h is not None and m4h >= -40.0 and
        p5 is not None and p5 >= 0.40 and
        p10 is not None and p10 >= 0.20
    )


def _make_events(rows: list[Dict[str, Any]]) -> list[list[Dict[str, Any]]]:
    buckets: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row, dict) and not row.get("allowed"):
            buckets[_identity_key(row)].append(row)
    events = []
    for rs in buckets.values():
        rs.sort(key=lambda r: (_num(r.get("scan")) if _num(r.get("scan")) is not None else 10**12))
        current: list[Dict[str, Any]] = []
        last = None
        for row in rs:
            scan = _num(row.get("scan"))
            if current and (scan is None or last is None or scan - last > MAX_SCAN_GAP):
                events.append(current)
                current = []
            current.append(row)
            last = scan
        if current:
            events.append(current)
    return events


def _event_best(event: list[Dict[str, Any]], horizon: int = HORIZON) -> float | None:
    values = [v for row in event if (v := _outcome(row, horizon)) is not None]
    return max(values) if values else None


def _reversal_analysis(data: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate score thresholds when ANY snapshot in an event matches the reversal shape."""
    events = _make_events(data.get("observations", []))
    shape_events = []
    for event in events:
        matches = [row for row in event if _reversal_shape(row)]
        if matches:
            # Use the earliest matching snapshot as the hypothetical entry point.
            entry = min(matches, key=lambda r: (_num(r.get("scan")) if _num(r.get("scan")) is not None else 10**12))
            best = _event_best(event)
            if best is not None:
                shape_events.append((entry, best, event))

    threshold_stats = {}
    for threshold in REVERSAL_SCORE_THRESHOLDS:
        selected = []
        for entry, best, _ in shape_events:
            score = _num((entry.get("features") or {}).get("score"))
            if score is not None and score >= threshold:
                selected.append(best)
        threshold_stats[str(int(threshold))] = {
            "count": len(selected),
            "wins_gt_0": sum(1 for x in selected if x > 0),
            "win_rate_pct": round(100 * sum(1 for x in selected if x > 0) / len(selected), 2) if selected else None,
            "avg_event_best_10h_net_return_pct": round(statistics.mean(selected), 3) if selected else None,
            "median_event_best_10h_net_return_pct": round(statistics.median(selected), 3) if selected else None,
            "best_event_10h_net_return_pct": round(max(selected), 3) if selected else None,
            "worst_event_10h_net_return_pct": round(min(selected), 3) if selected else None,
        }

    top = []
    for rank, (entry, best, event) in enumerate(sorted(shape_events, key=lambda x: x[1], reverse=True)[:10], 1):
        top.append({
            "rank": rank,
            "event_best_10h_net_return_pct": round(best, 3),
            "entry_identity": _identity(entry),
            "entry_features": _pick(entry),
            "entry_rejection_reasons": list(entry.get("rejection_reasons") or []),
            "event_snapshot_count": len(event),
        })

    return {
        "shape_definition": {
            "m1h_min": 8.0,
            "flow_min_exclusive": 0.0,
            "trades_min": 20.0,
            "m15_min": -8.0,
            "m15_max_exclusive": 0.0,
            "m4h_min": -40.0,
            "p5_min": 0.40,
            "p10_min": 0.20,
        },
        "shape_event_count": len(shape_events),
        "thresholds": threshold_stats,
        "top_shape_events": top,
        "note": "Observational only. Each event counts once; outcome is the event's best available +10h net return.",
    }


def build_report(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for row in data.get("observations", []):
        if not isinstance(row, dict) or row.get("allowed"):
            continue
        value = _outcome(row)
        if value is not None and value > THRESHOLD:
            rows.append((value, row))
    rows.sort(key=lambda item: item[0], reverse=True)

    candidates = [
        {
            "rank": rank,
            "net_return_10_pct": round(value, 2),
            "identity": _identity(row),
            "features": _pick(row),
            "rejection_reasons": list(row.get("rejection_reasons") or []),
        }
        for rank, (value, row) in enumerate(rows[:10], 1)
    ]

    feature_values: Dict[str, list[float]] = {}
    for _, row in rows[:10]:
        for key, value in _pick(row).items():
            feature_values.setdefault(key, []).append(value)

    return {
        "generated_at": time.time(),
        "source_observations": len(data.get("observations", [])),
        "horizon_hours": HORIZON,
        "threshold_net_return_pct": THRESHOLD,
        "count": len(rows),
        "top_10": candidates,
        "top_10_avg_features": {key: round(sum(values) / len(values), 3) for key, values in sorted(feature_values.items()) if values},
        "reversal_shape_analysis": _reversal_analysis(data),
        "note": "Observational only. This report does not alter V25 gate decisions or MAX-WIN.",
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
