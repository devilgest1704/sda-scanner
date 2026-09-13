"""V24 pump-path predictor (shadow/diagnostic only).

V23 predicts realized close ROI. V24 instead learns whether a setup reaches
meaningful upside after entry (MFE). Historical MFE is used only when the
position record contains a peak price captured after entry. Trades without a
recorded peak are deliberately excluded rather than being given a fabricated
label. MAE/time-to-threshold remain unavailable until the scanner records the
full post-entry path on every scan.
"""
from __future__ import annotations

import math
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
            "peak_price": 0.0,
            "has_peak": False,
        })
        g["closed_fraction"] += _num(tr.get("closed_fraction"), _num(tr.get("remaining_fraction"), 0.0))
        peak = _num(tr.get("trailing_peak_price"))
        if peak > 0:
            g["peak_price"] = max(g["peak_price"], peak)
            g["has_peak"] = True
    return [g for g in groups.values() if g["closed_fraction"] >= 0.999]


def historical_samples(closed_trades: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    groups = _group_complete_trades(closed_trades)
    samples: List[Dict[str, Any]] = []
    missing_peak = 0
    for tr in groups:
        entry = _num(tr.get("entry_price"))
        peak = _num(tr.get("peak_price"))
        metrics = tr.get("entry_metrics")
        if entry <= 0 or peak <= 0 or not isinstance(metrics, dict):
            missing_peak += 1
            continue
        mfe = (peak / entry - 1.0) * 100.0
        samples.append({"metrics": metrics, "mfe_pct": mfe, "entry_price": entry, "peak_price": peak})
    return samples, {"complete_trades": len(groups), "with_mfe": len(samples), "missing_mfe": missing_peak}


def predict(metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    samples, coverage = historical_samples(closed_trades)
    if len(samples) < MIN_SAMPLES:
        return {
            "version": "V24",
            "ready": False,
            "samples": len(samples),
            "coverage": coverage,
            "p5_mfe": 0.5,
            "p10_mfe": 0.2,
            "p20_mfe": 0.0,
            "p30_mfe": 0.0,
            "expected_mfe": 0.0,
            "mae_available": False,
            "time_to_pump_available": False,
        }

    current = _vector(metrics or {})
    ranked = sorted((_distance(current, _vector(s["metrics"])), s) for s in samples)
    nearest = ranked[:K]
    weights = [1.0 / (0.20 + d) for d, _ in nearest]
    total = sum(weights) or 1.0

    def probability(target: float) -> float:
        return sum(w for w, (_, s) in zip(weights, nearest) if s["mfe_pct"] >= target) / total

    expected = sum(w * s["mfe_pct"] for w, (_, s) in zip(weights, nearest)) / total
    quality = max(0.0, min(100.0, 100.0 * (0.30 * probability(10.0) + 0.30 * probability(20.0) + 0.20 * probability(30.0) + 0.20 * min(expected, 30.0) / 30.0)))
    return {
        "version": "V24",
        "ready": True,
        "samples": len(samples),
        "neighbors": len(nearest),
        "coverage": coverage,
        "p5_mfe": probability(5.0),
        "p10_mfe": probability(10.0),
        "p20_mfe": probability(20.0),
        "p30_mfe": probability(30.0),
        "expected_mfe": expected,
        "pump_quality": quality,
        "mae_available": False,
        "time_to_pump_available": False,
    }


def enrich(decision: Dict[str, Any], metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(decision or {})
    out["v24"] = predict(metrics or {}, closed_trades)
    return out
