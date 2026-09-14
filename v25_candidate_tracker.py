"""V25 candidate tracker.

Tracks relevant NO-BUY candidates as well as BUY candidates. Each observation
has a six-hour horizon. Completed observations are kept out of the pending
set until a cooldown expires, so the same token cannot immediately reset its
sample and prevent learning.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

STATE_FILE = "v25_candidate_state.json"
HORIZON_HOURS = 6
REENTRY_COOLDOWN_HOURS = 6
MAX_PENDING = 600
MAX_COMPLETED = 2000
MIN_VOLUME_1H = 250.0
MIN_SCORE = 45.0


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            x = json.load(f)
            return x if isinstance(x, dict) else {}
    except Exception:
        return {"version": "V25", "pending": {}, "completed": []}


def _save(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


def _price(analysis):
    return _num((analysis or {}).get("price_in_sda"))


def _features(score):
    keys = (
        "confidence", "buy_score", "m15", "m1h", "m4h", "net_1h",
        "net_15m", "net_4h", "whale_net", "whale_15m_net",
        "whale_4h_net", "trades_1h", "volume_1h", "technical_bull",
        "technical_bear", "v23_p5", "v23_p10", "v23_p20", "v23_p30",
        "v23_mean_roi", "v24_p5_mfe", "v24_p10_mfe", "v24_p20_mfe",
        "v24_p30_mfe", "v24_expected_mfe", "v24_expected_mae",
        "v24_pump_quality", "v25_p5", "v25_p10", "v25_p20", "v25_p30",
    )
    return {k: score.get(k) for k in keys if score.get(k) is not None}


def _update_path(row, price, now):
    entry = _num(row.get("entry_price"))
    if entry <= 0 or price <= 0:
        return False
    old_peak = _num(row.get("peak_price"), entry)
    old_trough = _num(row.get("trough_price"), entry)
    peak = max(old_peak, price)
    trough = min(old_trough, price)
    mfe = (peak / entry - 1.0) * 100.0
    mae = (trough / entry - 1.0) * 100.0
    changed = peak != old_peak or trough != old_trough
    row["peak_price"] = peak
    row["trough_price"] = trough
    row["mfe_pct"] = round(mfe, 6)
    row["mae_pct"] = round(mae, 6)
    hits = row.setdefault("threshold_times", {})
    for target in (5, 10, 20, 30):
        if mfe >= target and str(target) not in hits:
            hits[str(target)] = _iso(now)
            changed = True
    row["last_price"] = price
    row["last_seen_at"] = _iso(now)
    return changed


def _recently_completed(address, completed, now):
    cutoff = now - timedelta(hours=REENTRY_COOLDOWN_HOURS)
    for row in reversed(completed):
        if str(row.get("address", "")).lower() != address:
            continue
        completed_at = _parse_dt(row.get("completed_at")) or _parse_dt(row.get("last_seen_at"))
        return bool(completed_at and completed_at >= cutoff)
    return False


def update(paper_engine, scorer):
    """Track relevant candidates from the latest market snapshot."""
    try:
        md = paper_engine.load(paper_engine.MARKET_FILE, {"tokens": {}})
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        whale = paper_engine.load(paper_engine.WHALE_FILE, {})
        state = _load()
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else {}
        completed = state.get("completed") if isinstance(state.get("completed"), list) else []
        now = _now()
        changed = False

        # Finalize existing observations before discovering new ones.
        for address, row in list(pending.items()):
            token = tokens.get(str(address).lower(), {})
            analysis = token.get("analysis") if isinstance(token, dict) else None
            price = _price(analysis)
            if price > 0 and _update_path(row, price, now):
                changed = True
            started = _parse_dt(row.get("entry_at"))
            if started and now - started >= timedelta(hours=HORIZON_HOURS):
                row["completed_at"] = _iso(now)
                row["outcome"] = {
                    "mfe_pct": row.get("mfe_pct", 0.0),
                    "mae_pct": row.get("mae_pct", 0.0),
                    "threshold_times": row.get("threshold_times", {}),
                    "final_price": row.get("last_price", row.get("entry_price", 0.0)),
                }
                completed.append(row)
                del pending[address]
                changed = True

        candidates = []
        for address, token in tokens.items():
            if not isinstance(token, dict):
                continue
            analysis = token.get("analysis")
            if not isinstance(analysis, dict):
                continue
            address = str(address).lower()
            price = _price(analysis)
            if price <= 0:
                continue
            try:
                score = scorer(address, analysis, whale)
            except Exception:
                continue
            if not isinstance(score, dict):
                continue
            volume = _num(score.get("volume_1h", analysis.get("volume_1h")))
            buy_score = _num(score.get("buy_score", score.get("confidence")))
            trades = _num(score.get("trades_1h", analysis.get("trades_1h")))
            if volume < MIN_VOLUME_1H and buy_score < MIN_SCORE and trades < 5:
                continue
            candidates.append((buy_score, address, score, price))

        candidates.sort(key=lambda x: x[0], reverse=True)
        for buy_score, address, score, price in candidates[:MAX_PENDING]:
            if address in pending or _recently_completed(address, completed, now):
                continue
            pending[address] = {
                "version": "V25",
                "address": address,
                "entry_at": _iso(now),
                "entry_price": price,
                "peak_price": price,
                "trough_price": price,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "threshold_times": {},
                "buy_allowed": not bool(score.get("paper_buy_blocked")),
                "buy_block_reason": str(score.get("paper_buy_block_reason") or ""),
                "entry_metrics": _features(score),
                "entry_score": buy_score,
            }
            changed = True

        if len(completed) > MAX_COMPLETED:
            completed = completed[-MAX_COMPLETED:]
        state = {"version": "V25", "updated_at": _iso(now), "pending": pending, "completed": completed}
        if changed or not os.path.exists(STATE_FILE):
            _save(state)
        return {"pending": len(pending), "completed": len(completed), "changed": changed}
    except Exception:
        return {"pending": 0, "completed": 0, "changed": False, "error": "candidate tracker failed"}
