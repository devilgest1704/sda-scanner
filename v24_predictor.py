"""V24 pump-path predictor (shadow/diagnostic only).

V24 learns what happens after entry: maximum favorable excursion (MFE),
maximum adverse excursion (MAE), and time to meaningful upside. It never
changes the BUY/SL/TP decision.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

MIN_SAMPLES = 12
K = 12
TARGETS = (5.0, 10.0, 20.0, 30.0)
SCALES = (20.0, 10.0, 20.0, 20.0, 10.0, 10.0, 10.0, 10.0)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _vector(metrics: Dict[str, Any]) -> List[float]:
    return [
        _num(metrics.get("confidence")),
        _num(metrics.get("m15")),
        _num(metrics.get("m1h")),
        _num(metrics.get("m4h")),
        _num(metrics.get("net_1h")) / 1000.0,
        _num(metrics.get("whale_net")) / 1000.0,
        _num(metrics.get("whale_15m_net")) / 1000.0,
        _num(metrics.get("trades_1h")),
    ]


def _distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum(((x - y) / scale) ** 2 for x, y, scale in zip(a, b, SCALES)))


def _parse_time(value: Any):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _group_complete_trades(closed_trades: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for tr in closed_trades:
        if not isinstance(tr, dict):
            continue
        address = str(tr.get("address") or "").lower()
        opened = str(tr.get("opened_at") or "")
        if not address or not opened:
            continue
        key = (address, opened)
        g = groups.setdefault(key, {
            "address": address,
            "opened_at": opened,
            "investment_sda": _num(tr.get("investment_sda")),
            "entry_price": _num(tr.get("entry_price")),
            "entry_metrics": tr.get("entry_metrics") if isinstance(tr.get("entry_metrics"), dict) else {},
            "closed_fraction": 0.0,
            "mfe_pct": None,
            "mae_pct": None,
            "threshold_times": {},
        })
        g["closed_fraction"] += _num(tr.get("closed_fraction"), 0.0)
        if tr.get("v24_mfe_pct") is not None:
            value = _num(tr.get("v24_mfe_pct"), 0.0)
            g["mfe_pct"] = value if g["mfe_pct"] is None else max(g["mfe_pct"], value)
        if tr.get("v24_mae_pct") is not None:
            value = _num(tr.get("v24_mae_pct"), 0.0)
            g["mae_pct"] = value if g["mae_pct"] is None else min(g["mae_pct"], value)
        hits = tr.get("v24_threshold_times")
        if isinstance(hits, dict):
            for key2, value in hits.items():
                if value and key2 not in g["threshold_times"]:
                    g["threshold_times"][str(key2)] = value
    return [g for g in groups.values() if g["closed_fraction"] >= 0.999]


def historical_samples(closed_trades: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    groups = _group_complete_trades(closed_trades)
    samples: List[Dict[str, Any]] = []
    missing_path = 0
    for tr in groups:
        entry = _num(tr.get("entry_price"))
        mfe = tr.get("mfe_pct")
        if entry <= 0 or mfe is None:
            missing_path += 1
            continue
        mae = _num(tr.get("mae_pct"), 0.0)
        opened = _parse_time(tr.get("opened_at"))
        times = {}
        for target in TARGETS:
            hit = tr["threshold_times"].get(str(int(target)))
            ht = _parse_time(hit)
            if opened and ht:
                seconds = max(0.0, (ht - opened).total_seconds())
                times[str(int(target))] = seconds
        samples.append({
            "metrics": tr.get("entry_metrics") or {},
            "mfe_pct": _num(mfe),
            "mae_pct": mae,
            "times": times,
        })
    return samples, {"complete_trades": len(groups), "with_path": len(samples), "missing_path": missing_path}


def predict(metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    samples, coverage = historical_samples(closed_trades)
    if len(samples) < MIN_SAMPLES:
        return {
            "version": "V24", "ready": False, "samples": len(samples),
            "coverage": coverage, "p5_mfe": 0.5, "p10_mfe": 0.2,
            "p20_mfe": 0.0, "p30_mfe": 0.0, "expected_mfe": 0.0,
            "expected_mae": 0.0, "time_to_p10_min": None, "time_to_p20_min": None,
        }

    current = _vector(metrics or {})
    ranked = sorted((_distance(current, _vector(s["metrics"])), s) for s in samples)
    nearest = ranked[:K]
    weights = [1.0 / (0.20 + d) for d, _ in nearest]
    total = sum(weights) or 1.0

    def probability(target: float) -> float:
        return sum(w for w, (_, s) in zip(weights, nearest) if s["mfe_pct"] >= target) / total

    expected_mfe = sum(w * s["mfe_pct"] for w, (_, s) in zip(weights, nearest)) / total
    expected_mae = sum(w * s["mae_pct"] for w, (_, s) in zip(weights, nearest)) / total

    def weighted_time(target: str):
        pairs = [(w, s["times"].get(target)) for w, (_, s) in zip(weights, nearest) if s["times"].get(target) is not None]
        if not pairs:
            return None
        denom = sum(w for w, _ in pairs)
        return sum(w * seconds for w, seconds in pairs) / denom / 60.0

    p5, p10, p20, p30 = (probability(x) for x in TARGETS)
    quality = max(0.0, min(100.0, 100.0 * (
        0.20 * p5 + 0.30 * p10 + 0.30 * p20 + 0.20 * p30
    )))
    return {
        "version": "V24", "ready": True, "samples": len(samples), "neighbors": len(nearest),
        "coverage": coverage, "p5_mfe": p5, "p10_mfe": p10, "p20_mfe": p20, "p30_mfe": p30,
        "expected_mfe": expected_mfe, "expected_mae": expected_mae,
        "time_to_p10_min": weighted_time("10"), "time_to_p20_min": weighted_time("20"),
        "pump_quality": quality,
    }


def enrich(decision: Dict[str, Any], metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(decision or {})
    out["v24"] = predict(metrics or {}, closed_trades)
    return out
