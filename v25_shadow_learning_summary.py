"""Compact V25 Shadow Learning dataset scanner.

Reads the large observation file and writes a small JSON summary suitable for
routine inspection. This module is observational only and never changes V25
trading decisions or MAX-WIN.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from typing import Any, Dict

STATE_FILE = "v25_shadow_learning_v2.json"
SUMMARY_FILE = "v25_shadow_learning_summary.json"
HORIZONS = (5, 10, 20, 30)
FALSE_NEGATIVE_BANDS = (
    (0.0, "+0%"),
    (3.0, "+3%"),
    (5.0, "+5%"),
    (10.0, "+10%"),
)


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


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _reason_group(reason: Any) -> str:
    """Normalize dynamic gate messages into stable rule categories."""
    text = str(reason or "").strip().lower()
    if text.startswith("pump score"):
        return "PUMP_SCORE"
    if text.startswith("volume "):
        return "VOLUME"
    if text.startswith("trades "):
        return "TRADES"
    if text.startswith("m1h "):
        return "M1H"
    if text.startswith("m15 "):
        return "M15"
    if text.startswith("m4h "):
        return "M4H"
    if text.startswith("flow "):
        return "FLOW"
    if text.startswith("technical bear"):
        return "TECHNICAL_BEAR"
    if text.startswith("learner ev"):
        return "LEARNER_EV"
    if text.startswith("learner downside"):
        return "LEARNER_DOWNSIDE"
    if text.startswith("pump acceleration"):
        return "PUMP_ACCEL_PRESSURE"
    if not text:
        return "UNKNOWN"
    return str(reason).strip().upper()


def _outcome(row: Dict[str, Any], horizon: int):
    out = (row.get("outcomes") or {}).get(str(horizon))
    if not isinstance(out, dict):
        return None
    value = out.get("net_return_pct")
    return float(value) if isinstance(value, (int, float)) else None


def _lane_stats(rows):
    """Return horizon outcomes split by observational lane."""
    result = {}
    lanes = sorted({str(r.get("lane") or "UNKNOWN") for r in rows})
    for lane in lanes:
        lane_rows = [r for r in rows if str(r.get("lane") or "UNKNOWN") == lane]
        result[lane] = {}
        for h in HORIZONS:
            vals = [v for r in lane_rows if (v := _outcome(r, h)) is not None]
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


def _false_negative_stats(rows):
    """Find rejected observations that later became profitable."""
    result = {}
    for threshold, label in FALSE_NEGATIVE_BANDS:
        horizon = 10
        candidates = []
        for r in rows:
            if r.get("allowed"):
                continue
            value = _outcome(r, horizon)
            if value is not None and value > threshold:
                candidates.append(r)

        returns = [_outcome(r, horizon) for r in candidates]
        returns = [v for v in returns if v is not None]
        reason_counts = Counter()
        feature_groups = defaultdict(list)
        for r in candidates:
            for reason in (r.get("rejection_reasons") or []):
                reason_counts[_reason_group(reason)] += 1
            features = r.get("features") or {}
            for feature in (
                "pump_score", "score", "volume", "trades", "m1h", "m15",
                "m4h", "flow", "accel", "volume_ratio", "trade_ratio",
                "p5", "p10", "p20", "p30",
            ):
                value = _num(features.get(feature))
                if value is not None:
                    feature_groups[feature].append(value)

        result[label] = {
            "threshold_net_return_pct": threshold,
            "count": len(candidates),
            "avg_net_return_10_pct": _pct(returns),
            "median_net_return_10_pct": round(statistics.median(returns), 2) if returns else None,
            "best_net_return_10_pct": round(max(returns), 2) if returns else None,
            "rejection_groups": dict(reason_counts.most_common()),
            "avg_features": {
                feature: round(sum(values) / len(values), 3)
                for feature, values in sorted(feature_groups.items())
                if values
            },
        }
    return result


def _rejection_group_stats(rows):
    """Aggregate rejection reasons by stable gate rule, not dynamic value."""
    stats = defaultdict(lambda: {"count": 0, "completed_10": 0, "wins_10": 0, "returns": []})
    for r in rows:
        reasons = r.get("rejection_reasons") or []
        if not isinstance(reasons, list):
            continue
        value = _outcome(r, 10)
        for reason in reasons:
            group = _reason_group(reason)
            s = stats[group]
            s["count"] += 1
            if value is not None:
                s["completed_10"] += 1
                s["wins_10"] += int(value > 0)
                s["returns"].append(value)

    result = {}
    for group, s in sorted(stats.items(), key=lambda x: (-x[1]["count"], x[0])):
        result[group] = {
            "count": s["count"],
            "completed_10": s["completed_10"],
            "wins_10": s["wins_10"],
            "win_rate_10_pct": round(s["wins_10"] / s["completed_10"] * 100, 1) if s["completed_10"] else None,
            "avg_net_return_10_pct": _pct(s["returns"]),
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
        "rejection_groups": {},
        "false_negatives": {},
        "rule_impact": {},
    }

    result["lane_horizons"] = _lane_stats(rows)

    for h in HORIZONS:
        vals = []
        pass_vals = []
        reject_vals = []
        for r in rows:
            v = _outcome(r, h)
            if v is None:
                continue
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

    # Keep the original exact reasons for drill-down, but add normalized groups
    # so M15 -2.9% and M15 -3.2% are analyzed together as M15.
    exact_stats = defaultdict(lambda: {"count": 0, "completed": 0, "wins": 0, "returns": []})
    for r in rows:
        reasons = r.get("rejection_reasons") or []
        if not isinstance(reasons, list):
            continue
        value = _outcome(r, 10)
        for reason in reasons:
            key = str(reason)
            s = exact_stats[key]
            s["count"] += 1
            if value is not None:
                s["completed"] += 1
                s["wins"] += int(value > 0)
                s["returns"].append(value)
    for reason, s in sorted(exact_stats.items(), key=lambda x: (-x[1]["count"], x[0])):
        result["rejection_reasons"][reason] = {
            "count": s["count"],
            "completed_10": s["completed"],
            "wins_10": s["wins"],
            "win_rate_10_pct": round(s["wins"] / s["completed"] * 100, 1) if s["completed"] else None,
            "avg_net_return_10_pct": _pct(s["returns"]),
        }

    result["rejection_groups"] = _rejection_group_stats(rows)
    result["false_negatives"] = _false_negative_stats(rows)

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
            net = _outcome(r, 10)
            if isinstance(value, (int, float)) and net is not None:
                groups["at_or_above" if float(value) >= threshold else "below"].append(net)
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
