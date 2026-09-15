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
