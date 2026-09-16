"""False-negative drill-down report for V25 shadow learning.

Observational only: reads the compact shadow summary and the full shadow dataset
when available. It never changes V25 trading decisions or MAX-WIN.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any, Dict

STATE_FILE = "v25_shadow_learning_v2.json"
REPORT_FILE = "v25_shadow_learning_false_negative_report.json"
HORIZON = 10
THRESHOLD = 10.0
REVERSAL_SCORE_THRESHOLDS = (40.0, 45.0, 48.0, 50.0, 55.0, 60.0, 68.0)


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _outcome(row: Dict[str, Any]) -> float | None:
    out = (row.get("outcomes") or {}).get(str(HORIZON))
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
    result = {}
    for key in wanted:
        value = _num(features.get(key))
        if value is not None:
            result[key] = round(value, 3)
    return result


def _identity(row: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for key in ("address", "symbol", "token", "name", "scan", "lane", "allowed"):
        if key in row:
            result[key] = row.get(key)
    return result


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


def _reversal_analysis(data: Dict[str, Any]) -> Dict[str, Any]:
    """Empirically test the current reversal shape against score thresholds.

    One first observation per identity is used, matching the event-level
    'first_features' convention and avoiding repeated snapshots biasing counts.
    """
    first_by_identity: Dict[str, Dict[str, Any]] = {}
    for row in data.get("observations", []):
        if not isinstance(row, dict) or row.get("allowed"):
            continue
        key = str(row.get("address") or row.get("token") or row.get("symbol") or row.get("name") or "UNKNOWN")
        if key not in first_by_identity:
            first_by_identity[key] = row
    shape_rows = [r for r in first_by_identity.values() if _reversal_shape(r)]
    shape_rows.sort(key=lambda r: (_outcome(r) if _outcome(r) is not None else -10**9), reverse=True)

    threshold_stats = {}
    for threshold in REVERSAL_SCORE_THRESHOLDS:
        selected = []
        for row in shape_rows:
            score = _num((row.get("features") or {}).get("score"))
            outcome = _outcome(row)
            if score is not None and score >= threshold and outcome is not None:
                selected.append(outcome)
        threshold_stats[str(int(threshold))] = {
            "count": len(selected),
            "wins_gt_0": sum(1 for x in selected if x > 0),
            "win_rate_pct": round(100 * sum(1 for x in selected if x > 0) / len(selected), 2) if selected else None,
            "avg_best_10h_net_return_pct": round(statistics.mean(selected), 3) if selected else None,
            "median_best_10h_net_return_pct": round(statistics.median(selected), 3) if selected else None,
            "best_10h_net_return_pct": round(max(selected), 3) if selected else None,
            "worst_10h_net_return_pct": round(min(selected), 3) if selected else None,
        }

    top = []
    for rank, row in enumerate(shape_rows[:10], 1):
        top.append({
            "rank": rank,
            "net_return_10_pct": round(_outcome(row), 3) if _outcome(row) is not None else None,
            "identity": _identity(row),
            "features": _pick(row),
            "rejection_reasons": list(row.get("rejection_reasons") or []),
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
        "first_observation_identities": len(first_by_identity),
        "shape_count": len(shape_rows),
        "thresholds": threshold_stats,
        "top_shape_events": top,
        "note": "Observational only. This analysis does not alter V25 gate decisions or MAX-WIN.",
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

    candidates = []
    for rank, (value, row) in enumerate(rows[:10], 1):
        candidates.append({
            "rank": rank,
            "net_return_10_pct": round(value, 2),
            "identity": _identity(row),
            "features": _pick(row),
            "rejection_reasons": list(row.get("rejection_reasons") or []),
        })

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
        "top_10_avg_features": {
            key: round(sum(values) / len(values), 3)
            for key, values in sorted(feature_values.items())
            if values
        },
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
