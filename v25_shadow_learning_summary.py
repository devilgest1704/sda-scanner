"""Compact V25 Shadow Learning dataset scanner.

Reads the large observation file and writes a small JSON summary suitable for
routine inspection. This module is observational only and never changes V25
trading decisions or MAX-WIN.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Dict

STATE_FILE = "v25_shadow_learning_v2.json"
SUMMARY_FILE = "v25_shadow_learning_summary.json"
HORIZONS = (5, 10, 20, 30)


def _load() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pct(values):
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _lane_stats(rows):
    """Return horizon outcomes split by observational lane."""
    result = {}
    lanes = sorted({str(r.get("lane") or "UNKNOWN") for r in rows})
    for lane in lanes:
        lane_rows = [r for r in rows if str(r.get("lane") or "UNKNOWN") == lane]
        result[lane] = {}
        for h in HORIZONS:
            vals = []
            for r in lane_rows:
                out = (r.get("outcomes") or {}).get(str(h))
                value = out.get("net_return_pct") if isinstance(out, dict) else None
                if isinstance(value, (int, float)):
                    vals.append(float(value))
            if vals:
                wins = sum(v > 0 for v in vals)
                result[lane][str(h)] = {
                    "completed": len(vals),
                    "wins": wins,
                    "win_rate_pct": round(wins / len(vals) * 100, 1),
                    "avg_net_return_pct": _pct(vals),
                    "median_net_return_pct": round(statistics.median(vals), 2),
                }
    return result


def build_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = [r for r in data.get("observations", []) if isinstance(r, dict)]
    result: Dict[str, Any] = {
        "generated_at": time.time(),
        "scan_counter": int(data.get("scan_counter") or 0),
        "observations": len(rows),
        "lanes": dict(Counter(str(r.get("lane") or "UNKNOWN") for r in rows)),
        "lane_horizons": {},
        "last_near_pass": int(data.get("last_near_pass") or 0),
        "horizons": {},
        "rejection_reasons": {},
        "rule_impact": {},
    }

    result["lane_horizons"] = _lane_stats(rows)

    for h in HORIZONS:
        vals = []
        pass_vals = []
        reject_vals = []
        for r in rows:
            out = (r.get("outcomes") or {}).get(str(h))
            if not isinstance(out, dict):
                continue
            v = out.get("net_return_pct")
            if not isinstance(v, (int, float)):
                continue
            v = float(v)
            vals.append(v)
            (pass_vals if r.get("allowed") else reject_vals).append(v)
        if vals:
            result["horizons"][str(h)] = {
                "completed": len(vals),
                "wins": sum(v > 0 for v in vals),
                "win_rate_pct": round(sum(v > 0 for v in vals) / len(vals) * 100, 1),
                "avg_net_return_pct": _pct(vals),
                "median_net_return_pct": round(statistics.median(vals), 2),
                "best_net_return_pct": round(max(vals), 2),
                "worst_net_return_pct": round(min(vals), 2),
                "pass_avg_net_return_pct": _pct(pass_vals),
                "reject_avg_net_return_pct": _pct(reject_vals),
                "reject_wins": sum(v > 0 for v in reject_vals),
            }

    reason_stats = defaultdict(lambda: {"count": 0, "completed": 0, "wins": 0, "returns": []})
    for r in rows:
        reasons = r.get("rejection_reasons") or []
        if not isinstance(reasons, list):
            continue
        out = (r.get("outcomes") or {}).get("10")
        value = out.get("net_return_pct") if isinstance(out, dict) else None
        for reason in reasons:
            key = str(reason)
            s = reason_stats[key]
            s["count"] += 1
            if isinstance(value, (int, float)):
                s["completed"] += 1
                s["wins"] += int(float(value) > 0)
                s["returns"].append(float(value))
    for reason, s in sorted(reason_stats.items(), key=lambda x: (-x[1]["count"], x[0])):
        result["rejection_reasons"][reason] = {
            "count": s["count"],
            "completed_10": s["completed"],
            "wins_10": s["wins"],
            "win_rate_10_pct": round(s["wins"] / s["completed"] * 100, 1) if s["completed"] else None,
            "avg_net_return_10_pct": _pct(s["returns"]),
        }

    # Feature threshold diagnostics: compare observations below/at and above
    # current base gate levels. This is descriptive, not an optimizer.
    thresholds = {
        "pump_score": 55.0,
        "score": 55.0,
        "volume": 250.0,
        "trades": 7.0,
        "m1h": 3.0,
        "m15": 0.25,
        "m4h": 0.0,
        "flow": 0.0,
        "accel": 5.0,
    }
    for feature, threshold in thresholds.items():
        groups = {"below": [], "at_or_above": []}
        for r in rows:
            value = (r.get("features") or {}).get(feature)
            out = (r.get("outcomes") or {}).get("10")
            net = out.get("net_return_pct") if isinstance(out, dict) else None
            if isinstance(value, (int, float)) and isinstance(net, (int, float)):
                groups["at_or_above" if float(value) >= threshold else "below"].append(float(net))
        result["rule_impact"][feature] = {
            "threshold": threshold,
            "below_n": len(groups["below"]),
            "below_avg_net_10_pct": _pct(groups["below"]),
            "below_win_rate_10_pct": round(sum(v > 0 for v in groups["below"]) / len(groups["below"]) * 100, 1) if groups["below"] else None,
            "at_or_above_n": len(groups["at_or_above"]),
            "at_or_above_avg_net_10_pct": _pct(groups["at_or_above"]),
            "at_or_above_win_rate_10_pct": round(sum(v > 0 for v in groups["at_or_above"]) / len(groups["at_or_above"]) * 100, 1) if groups["at_or_above"] else None,
        }
    return result


def write_summary() -> Dict[str, Any]:
    summary = build_summary(_load())
    tmp = SUMMARY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, SUMMARY_FILE)
    return summary
