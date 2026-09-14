"""V25 adaptive online learner for pump hunting (shadow-only).

Learns from completed paper trades AND completed candidate observations,
including candidates that the BUY filter rejected. The objective is expected
post-fee P/L, while MFE/MAE and pump thresholds are tracked separately.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

STATE_FILE = "v25_learner_state.json"
CANDIDATE_STATE_FILE = "v25_candidate_state.json"
MIN_SAMPLES = 20
K = 20
TARGETS = (5.0, 10.0, 20.0, 30.0)
SCALES = (20.0, 10.0, 20.0, 20.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0)
INVESTMENT_SDA = 50.0
FEE_RATE = 0.01
SLIPPAGE_RATE = 0.001


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _vector(m: Dict[str, Any]) -> List[float]:
    return [
        _num(m.get("confidence")), _num(m.get("m15")), _num(m.get("m1h")),
        _num(m.get("m4h")), _num(m.get("net_1h")) / 1000.0,
        _num(m.get("net_15m")) / 1000.0, _num(m.get("whale_net")) / 1000.0,
        _num(m.get("whale_15m_net")) / 1000.0, _num(m.get("trades_1h")),
        _num(m.get("volume_1h")) / 1000.0,
        _num(m.get("v23_p10")), _num(m.get("v24_expected_mfe")),
    ]


def _distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum(((x - y) / s) ** 2 for x, y, s in zip(a, b, SCALES)))


def _load() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _save(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _candidate_samples() -> List[Dict[str, Any]]:
    try:
        with open(CANDIDATE_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        completed = state.get("completed", []) if isinstance(state, dict) else []
    except Exception:
        return []
    out = []
    for row in completed:
        if not isinstance(row, dict):
            continue
        entry = _num(row.get("entry_price")); final = _num(row.get("last_price"), entry)
        if entry <= 0 or final <= 0:
            continue
        proceeds = INVESTMENT_SDA * (final / entry) * (1.0 - SLIPPAGE_RATE) * (1.0 - FEE_RATE)
        cost = INVESTMENT_SDA * (1.0 + FEE_RATE)
        profit = proceeds - cost
        out.append({"metrics": dict(row.get("entry_metrics") or {}), "profit_sda": profit,
                    "mfe": _num(row.get("mfe_pct")), "mae": _num(row.get("mae_pct")),
                    "times": dict(row.get("threshold_times") or {}), "candidate": True,
                    "buy_allowed": bool(row.get("buy_allowed"))})
    return out


def _complete(closed: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for tr in closed:
        if not isinstance(tr, dict):
            continue
        key = (str(tr.get("address") or "").lower(), str(tr.get("opened_at") or ""))
        if not key[0] or not key[1]:
            continue
        g = groups.setdefault(key, {"entry_metrics": tr.get("entry_metrics") or {}, "profit": 0.0, "fraction": 0.0, "mfe": None, "mae": None, "times": {}})
        g["profit"] += _num(tr.get("closed_profit_sda"))
        g["fraction"] += _num(tr.get("closed_fraction"))
        if tr.get("v24_mfe_pct") is not None:
            x = _num(tr.get("v24_mfe_pct")); g["mfe"] = x if g["mfe"] is None else max(g["mfe"], x)
        if tr.get("v24_mae_pct") is not None:
            x = _num(tr.get("v24_mae_pct")); g["mae"] = x if g["mae"] is None else min(g["mae"], x)
        hits = tr.get("v24_threshold_times")
        if isinstance(hits, dict):
            for k, v in hits.items():
                if v and k not in g["times"]: g["times"][k] = v
    return [g for g in groups.values() if g["fraction"] >= 0.999 and g["mfe"] is not None]


def _build_samples(closed: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for tr in _complete(closed):
        m = dict(tr.get("entry_metrics") or {})
        out.append({"metrics": m, "profit_sda": tr["profit"], "mfe": _num(tr["mfe"]), "mae": _num(tr["mae"]), "times": tr["times"], "candidate": False})
    out.extend(_candidate_samples())
    return out


def predict(metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    samples = _build_samples(closed_trades)
    coverage = len(samples)
    candidate_count = len(_candidate_samples())
    if coverage < MIN_SAMPLES:
        return {"version":"V25","ready":False,"samples":coverage,"neighbors":0,"expected_pl_sda":0.0,"p5":0.0,"p10":0.0,"p20":0.0,"p30":0.0,"expected_mfe":0.0,"expected_mae":0.0,"learning_mode":"shadow","candidate_samples":candidate_count}
    cur = _vector(metrics or {})
    ranked = sorted((_distance(cur, _vector(s["metrics"])), s) for s in samples)[:K]
    weights = [1.0 / (0.25 + d) for d, _ in ranked]
    denom = sum(weights) or 1.0
    def wavg(key): return sum(w * _num(s[key]) for w, (_, s) in zip(weights, ranked)) / denom
    def prob(target): return sum(w for w, (_, s) in zip(weights, ranked) if _num(s["mfe"]) >= target) / denom
    candidate_neighbors = sum(1 for _, s in ranked if s.get("candidate"))
    return {"version":"V25","ready":True,"samples":coverage,"neighbors":len(ranked),"candidate_samples":candidate_count,"candidate_neighbors":candidate_neighbors,
            "expected_pl_sda":round(wavg("profit_sda"),6),"p5":prob(5.0),"p10":prob(10.0),"p20":prob(20.0),"p30":prob(30.0),
            "expected_mfe":wavg("mfe"),"expected_mae":wavg("mae"),"learning_mode":"shadow"}


def enrich(decision: Dict[str, Any], metrics: Dict[str, Any], closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(decision or {})
    out["v25"] = predict(metrics or {}, closed_trades)
    return out


def learn(closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    # This runs even when the paper engine opens zero BUY positions.
    try:
        import main as scanner
        import v25_candidate_tracker
        v25_candidate_tracker.update(scanner._paper, scanner._paper_score_predictive)
    except Exception:
        pass
    samples = _build_samples(closed_trades)
    state = _load()
    state.update({"version":"V25","updated_at":datetime.now(timezone.utc).isoformat(),"samples":len(samples),"candidate_samples":len(_candidate_samples()),"mode":"shadow","objective":"expected_post_fee_pnl","source_mix":"paper_trades+missed_candidates"})
    _save(state)
    return state
