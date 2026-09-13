"""V23 pump-oriented predictor helpers.

This module is deliberately side-effect free.  It fixes the biggest training-data
problem in the previous predictor: partial close events are grouped back into
one real trade before becoming a training sample.
"""
from __future__ import annotations
import json
import math
from collections import defaultdict

POSITIONS_FILE = "positions.json"
MIN_SAMPLES = 12
K = 12


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_positions(path=POSITIONS_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clean_trades(positions=None):
    """Aggregate partial close events into one trade-level sample."""
    positions = positions if isinstance(positions, dict) else load_positions()
    groups = defaultdict(list)
    for row in positions.get("closed_trades", []) or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("address") or "").lower(), str(row.get("opened_at") or ""))
        if key[0] and key[1]:
            groups[key].append(row)

    result = []
    for (address, opened_at), rows in groups.items():
        entry = next((r for r in rows if isinstance(r.get("entry_metrics"), dict)), rows[0])
        metrics = entry.get("entry_metrics") or {}
        investment = _num(entry.get("investment_sda"), 0.0)
        profit = sum(_num(r.get("closed_profit_sda"), 0.0) for r in rows)
        closed_fraction = sum(max(0.0, _num(r.get("closed_fraction"), 0.0)) for r in rows)
        # A trade is complete once the recorded close fractions add up to the
        # original position. Partial-only records are excluded from training.
        complete = closed_fraction >= 0.999
        if not complete or investment <= 0 or not metrics:
            continue
        roi = profit / investment * 100.0
        result.append({
            "address": address,
            "opened_at": opened_at,
            "label": entry.get("label") or address,
            "entry_metrics": metrics,
            "profit_sda": profit,
            "investment_sda": investment,
            "roi_pct": roi,
            "close_reasons": [str(r.get("close_reason") or "") for r in rows],
            "events": len(rows),
        })
    return result


def _feature(metrics):
    m = metrics if isinstance(metrics, dict) else {}
    # The historical scanner already records volume acceleration.  net_15m is
    # also available on older samples, so use it as a short-window flow proxy.
    net1 = _num(m.get("net_1h"))
    net15 = _num(m.get("net_15m"))
    trades = _num(m.get("trades_1h"))
    whale = _num(m.get("whale_net"))
    whale15 = _num(m.get("whale_15m_net"))
    accel = _num(m.get("volume_accel", m.get("volume_acceleration_15m_pct")))
    return [
        _num(m.get("confidence")),
        _num(m.get("m15")),
        _num(m.get("m1h")),
        _num(m.get("m4h")),
        net1 / 1000.0,
        net15 / 1000.0,
        whale / 1000.0,
        whale15 / 1000.0,
        trades,
        accel / 20.0,
    ]


_SCALES = [20.0, 10.0, 20.0, 20.0, 10.0, 5.0, 10.0, 10.0, 10.0, 5.0]


def _distance(a, b):
    return math.sqrt(sum(((x - y) / scale) ** 2 for x, y, scale in zip(a, b, _SCALES)))


def pump_score(metrics):
    """Deterministic setup quality score, used for diagnostics and ranking.

    This is intentionally not a BUY gate yet.  V23 first observes this signal
    alongside the existing MAX-WIN gate so we can validate it on live scans.
    """
    m15 = _num(metrics.get("m15")); m1 = _num(metrics.get("m1h")); m4 = _num(metrics.get("m4h"))
    flow = _num(metrics.get("net_1h")); flow15 = _num(metrics.get("net_15m"))
    accel = _num(metrics.get("volume_accel", metrics.get("volume_acceleration_15m_pct")))
    trades = _num(metrics.get("trades_1h")); whale15 = _num(metrics.get("whale_15m_net"))

    score = 50.0
    # Early acceleration is rewarded; extreme old momentum without short-term
    # confirmation is penalized as potential exhaustion.
    score += max(-15.0, min(15.0, accel * 0.35))
    score += max(-10.0, min(10.0, m15 * 0.9))
    score += max(-8.0, min(8.0, flow15 / 300.0))
    score += 6.0 if flow > 0 else -6.0
    score += 5.0 if whale15 > 0 else (-3.0 if whale15 < 0 else 0.0)
    score += min(8.0, trades / 8.0)
    if m1 > 25 and m15 < 0:
        score -= 12.0
    if m4 > 35 and m1 > 25 and m15 < 3:
        score -= 8.0
    if m15 > 0 and m1 > m15:
        score += 4.0
    return max(0.0, min(100.0, score))


def predict(metrics, positions=None):
    trades = clean_trades(positions)
    current = _feature(metrics)
    samples = [(_distance(current, _feature(t["entry_metrics"])), t["roi_pct"]) for t in trades]
    if len(samples) < MIN_SAMPLES:
        return {
            "ready": False, "samples": len(samples), "neighbors": 0,
            "p5": 0.5, "p10": 0.2, "p20": 0.0, "p30": 0.0,
            "mean_roi": 0.0, "pump_score": pump_score(metrics),
            "training_mode": "clean_trade_warmup",
        }
    samples.sort(key=lambda x: x[0])
    nearest = samples[:K]
    weights = [1.0 / (0.20 + d) for d, _ in nearest]
    sw = sum(weights) or 1.0
    def prob(target):
        return sum(w for w, (_, roi) in zip(weights, nearest) if roi >= target) / sw
    mean_roi = sum(w * roi for w, (_, roi) in zip(weights, nearest)) / sw
    return {
        "ready": True,
        "samples": len(samples), "neighbors": len(nearest),
        "p5": prob(5.0), "p10": prob(10.0), "p20": prob(20.0), "p30": prob(30.0),
        "mean_roi": mean_roi,
        "pump_score": pump_score(metrics),
        "training_mode": "clean_trade_nearest_neighbor",
    }


def enrich(score_data, analysis=None):
    """Add V23 diagnostics without changing the existing BUY decision."""
    s = dict(score_data or {})
    metrics = dict(s)
    if isinstance(analysis, dict):
        accel = analysis.get("volume_acceleration_15m_pct")
        if accel is not None:
            metrics.setdefault("volume_accel", accel)
        flow = analysis.get("flow") or {}
        f1 = flow.get("1h") or {}
        f15 = flow.get("15m") or {}
        metrics.setdefault("net_1h", f1.get("net_flow"))
        metrics.setdefault("net_15m", f15.get("net_flow"))
        metrics.setdefault("trades_1h", _num(f1.get("buy_count")) + _num(f1.get("sell_count")))
    pred = predict(metrics)
    s["v23_prediction"] = pred
    s["v23_p5"] = pred.get("p5", 0.0)
    s["v23_p10"] = pred.get("p10", 0.0)
    s["v23_p20"] = pred.get("p20", 0.0)
    s["v23_p30"] = pred.get("p30", 0.0)
    s["v23_mean_roi"] = pred.get("mean_roi", 0.0)
    s["v23_pump_score"] = pred.get("pump_score", 0.0)
    s["v23_training_mode"] = pred.get("training_mode", "")
    return s
