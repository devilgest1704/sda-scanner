"""Fail-closed MAX_HOLD_STAGNANT_PROFIT policy and bounded REAL-only history."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

MIN_HOLD_SECONDS = 72 * 3600
STAGNATION_WINDOW_SECONDS = 24 * 3600
MIN_IMPROVEMENT_PP = 1.0
MIN_HISTORY_COVERAGE = 0.75
MAX_PRICE_AGE_SECONDS = 300
MAX_TREND_SNAPSHOT_AGE_SECONDS = 20 * 60
MAX_HISTORY_GAP_SECONDS = 6 * 3600
HISTORY_RETENTION_SECONDS = 26 * 3600
HISTORY_SAMPLE_INTERVAL_SECONDS = 240
MAX_HISTORY_POINTS = 400
MAX_HISTORY_POSITIONS = 50


def _time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return result.astimezone(UTC) if result.tzinfo else None


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def record_observation(state, token, position, price, pnl_pct, timestamp, now=None):
    """Persist fresh observations per REAL position; bounded and restart-safe."""
    now = now or datetime.now(UTC)
    stamp = _time(timestamp)
    px, pnl = _number(price), _number(pnl_pct)
    if stamp is None or px is None or px <= 0 or pnl is None:
        return []
    age = (now.astimezone(UTC) - stamp).total_seconds()
    if age < 0 or age > MAX_PRICE_AGE_SECONDS:
        return []
    identity = str(position.get("position_id") or position.get("buy_tx") or token)
    key = f"{str(token).lower()}:{identity}"
    root = state.setdefault("real_stagnation_history", {})
    cutoff = now.astimezone(UTC) - timedelta(seconds=HISTORY_RETENTION_SECONDS)
    live_positions = state.get("positions") or {}
    for old_key, old_record in list(root.items()):
        if not isinstance(old_record, dict):
            root.pop(old_key, None)
            continue
        old_token = str(old_record.get("token") or "").lower()
        old_identity = str(old_record.get("position_id") or "")
        live = live_positions.get(old_token)
        live_identity = (str(live.get("position_id") or live.get("buy_tx") or old_token)
                         if isinstance(live, dict) else "")
        if not live_identity or live_identity != old_identity:
            root.pop(old_key, None)
            continue
        old_observations = old_record.get("observations") or []
        old_record["observations"] = [
            item for item in old_observations
            if _time(item.get("timestamp")) and _time(item.get("timestamp")) >= cutoff
        ]
        if not old_record["observations"]:
            root.pop(old_key, None)
    record = root.setdefault(key, {"token": str(token).lower(),
                                   "position_id": identity, "observations": []})
    observations = record.setdefault("observations", [])
    if observations:
        last_observation = observations[-1]
        last = _time(last_observation.get("timestamp"))
        if last and 0 <= (stamp - last).total_seconds() < HISTORY_SAMPLE_INTERVAL_SECONDS:
            # Keep both the latest mark (for the baseline) and bucket maximum
            # (for stagnation detection) without writing a dense time series.
            old_max = _number(last_observation.get("max_pnl_pct"))
            old_latest = _number(last_observation.get("pnl_pct"))
            bucket_max = max(value for value in (old_max, old_latest, pnl) if value is not None)
            last_observation.update({
                "timestamp": stamp.isoformat(), "price": str(px),
                "pnl_pct": float(pnl), "max_pnl_pct": float(bucket_max),
            })
            return observations
        if last and stamp < last:
            return observations
    observations.append({"timestamp": stamp.isoformat(), "price": str(px),
                         "pnl_pct": float(pnl), "max_pnl_pct": float(pnl)})
    observations[:] = [item for item in observations
                       if _time(item.get("timestamp")) and _time(item.get("timestamp")) >= cutoff]
    if len(observations) > MAX_HISTORY_POINTS:
        # Preserve endpoints while thinning dense worker observations.
        stride = max(1, (len(observations) - 1) // (MAX_HISTORY_POINTS - 1))
        thinned = observations[::stride]
        if thinned[-1] != observations[-1]:
            thinned.append(observations[-1])
        observations[:] = thinned[-MAX_HISTORY_POINTS:]
    if len(root) > MAX_HISTORY_POSITIONS:
        newest = sorted(
            root.items(),
            key=lambda pair: _time((pair[1].get("observations") or [{}])[-1].get("timestamp"))
                or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        keep = {item[0] for item in newest[:MAX_HISTORY_POSITIONS]}
        for old_key in list(root):
            if old_key not in keep:
                root.pop(old_key, None)
    return observations


def assess_trend(context, now=None):
    """Validate production market-analysis freshness and detect any bullish recovery."""
    now = now or datetime.now(UTC)
    context = context if isinstance(context, dict) else {}
    snapshot = _time(context.get("_snapshot_updated_at"))
    if snapshot is None or (now - snapshot).total_seconds() < 0 or (now - snapshot).total_seconds() > MAX_TREND_SNAPSHOT_AGE_SECONDS:
        return {"state": "UNKNOWN", "fresh": False, "bullish": False,
                "blocker": "STALE_TREND", "momentum_1h": None, "momentum_4h": None,
                "macd_state": "UNKNOWN", "flow_1h": None, "flow_4h": None}
    momentum = context.get("momentum") or {}
    flow = context.get("flow") or {}
    technical = context.get("technical") or {}
    m1, m4 = _number(momentum.get("1h_pct")), _number(momentum.get("4h_pct"))
    f1, f4 = flow.get("1h") or {}, flow.get("4h") or {}
    n1, n4 = _number(f1.get("net_flow")), _number(f4.get("net_flow"))
    tx1, tx4 = _number(f1.get("transactions")), _number(f4.get("transactions"))
    t1, t4 = technical.get("1h") or {}, technical.get("4h") or {}
    c1, c4 = _time(t1.get("last_timestamp")), _time(t4.get("last_timestamp"))
    age1 = (now - c1).total_seconds() if c1 else float("inf")
    age4 = (now - c4).total_seconds() if c4 else float("inf")
    if (m1 is None or m4 is None or n1 is None or n4 is None or
            not t1.get("available") or not t4.get("available") or
            age1 < 0 or age1 > 2 * 3600 or age4 < 0 or age4 > 5 * 3600):
        return {"state": "UNKNOWN", "fresh": False, "bullish": False,
                "blocker": "STALE_TREND", "momentum_1h": float(m1) if m1 is not None else None,
                "momentum_4h": float(m4) if m4 is not None else None,
                "macd_state": "UNKNOWN", "flow_1h": float(n1) if n1 is not None else None,
                "flow_4h": float(n4) if n4 is not None else None}
    macd1, macd4 = (t1.get("macd") or {}), (t4.get("macd") or {})
    macd_rows = [m for m in (macd1, macd4) if isinstance(m, dict)]
    macd_positive = any(
        bool(m.get("bullish")) or bool(m.get("histogram_rising"))
        for m in macd_rows
    )
    macd_rising = any(bool(m.get("histogram_rising")) for m in macd_rows)
    macd_state = "BULLISH_RISING" if macd_positive and macd_rising else (
        "BULLISH" if macd_positive else "NON_BULLISH")
    trend_1h = str(t1.get("trend") or "UNKNOWN").upper()
    trend_4h = str(t4.get("trend") or "UNKNOWN").upper()
    bullish_trend_label = any(label.startswith("BULLISH") for label in (trend_1h, trend_4h))
    positive_flow = ((n1 > 0 and (tx1 or 0) > 0) or (n4 > 0 and (tx4 or 0) > 0))
    breakout = context.get("breakout_volume_trend") or {}
    bullish_impulse = bool(breakout.get("eligible")) and (
        (_number(breakout.get("breakout_pct")) or Decimal(0)) > 0)
    bullish = bool(m1 >= Decimal("0.5") or m4 >= Decimal("1.0") or
                   macd_positive or bullish_trend_label or positive_flow or bullish_impulse)
    return {
        "state": "POSITIVE" if bullish else "FLAT",
        "fresh": True, "bullish": bullish, "blocker": "POSITIVE_MOMENTUM" if bullish else None,
        "momentum_1h": float(m1), "momentum_4h": float(m4),
        "macd_state": macd_state, "technical_trend_1h": trend_1h,
        "technical_trend_4h": trend_4h, "flow_1h": float(n1), "flow_4h": float(n4),
    }


def evaluate(position, *, now, price, price_timestamp, history, trend,
             current_pnl_pct, net_exit_pnl_sda=None, pending_sell=False):
    """Evaluate every gate; age alone can never produce a SELL."""
    result = {"decision": "HOLD", "blocker": None, "eligible_without_net": False,
              "improvement_pp": None, "history_coverage_pct": 0.0,
              "pnl_24h_start_pct": None, "pnl_24h_max_pct": None}
    if pending_sell:
        result["blocker"] = "PENDING_SELL_INTENT"
        return result
    if position.get("sellManagementEnabled") is not True:
        result["blocker"] = "SELL_MANAGEMENT_DISABLED"
        return result
    if position.get("profit_partial_done"):
        result["blocker"] = "PARTIAL_ALREADY_DONE"
        return result
    if position.get("trailing_armed"):
        result["blocker"] = "TRAILING_ALREADY_ARMED"
        return result
    opened = _time(position.get("opened_at") or position.get("at") or position.get("buy_timestamp"))
    if opened is None or (now - opened).total_seconds() < MIN_HOLD_SECONDS:
        result["blocker"] = "MIN_HOLD_NOT_REACHED"
        return result
    pnl = _number(current_pnl_pct)
    if pnl is None or pnl <= 0:
        result["blocker"] = "NON_POSITIVE_PNL"
        return result
    px = _number(price)
    stamp = _time(price_timestamp)
    price_age = (now - stamp).total_seconds() if stamp else float("inf")
    if px is None or px <= 0 or price_age < 0 or price_age > MAX_PRICE_AGE_SECONDS:
        result["blocker"] = "STALE_PRICE"
        return result
    if not isinstance(trend, dict) or not trend.get("fresh"):
        result["blocker"] = "STALE_TREND"
        return result
    if trend.get("bullish"):
        result["blocker"] = "POSITIVE_MOMENTUM"
        return result
    window_start = now - timedelta(seconds=STAGNATION_WINDOW_SECONDS)
    points = []
    for item in history or []:
        stamp = _time(item.get("timestamp"))
        value = _number(item.get("pnl_pct"))
        bucket_max = _number(item.get("max_pnl_pct")) or value
        if stamp is None or value is None or bucket_max is None or stamp > now or stamp < window_start:
            continue
        points.append((stamp, value, bucket_max))
    points.sort(key=lambda row: row[0])
    if len(points) < 4:
        result["blocker"] = "INSUFFICIENT_HISTORY"
        return result
    coverage = (now - points[0][0]).total_seconds() / STAGNATION_WINDOW_SECONDS
    gaps = [(points[i][0] - points[i - 1][0]).total_seconds()
            for i in range(1, len(points))]
    if coverage < MIN_HISTORY_COVERAGE or (gaps and max(gaps) > MAX_HISTORY_GAP_SECONDS):
        result["blocker"] = "INSUFFICIENT_HISTORY"
        result["history_coverage_pct"] = round(max(0.0, coverage) * 100, 2)
        return result
    maximum_pnl = max(point[2] for point in points)
    improvement = maximum_pnl - points[0][1]
    result.update({
        "history_coverage_pct": round(min(1.0, coverage) * 100, 2),
        "pnl_24h_start_pct": float(points[0][1]),
        "pnl_24h_max_pct": float(maximum_pnl),
        "improvement_pp": float(improvement),
    })
    if improvement >= Decimal(str(MIN_IMPROVEMENT_PP)):
        result["blocker"] = "PROFIT_IMPROVED"
        return result
    result["eligible_without_net"] = True
    if net_exit_pnl_sda is None:
        result["blocker"] = "NET_EXIT_QUOTE_REQUIRED"
        return result
    net = _number(net_exit_pnl_sda)
    if net is None or net <= 0:
        result["blocker"] = "NET_EXIT_NOT_PROFITABLE"
        return result
    result.update({"decision": "MAX_HOLD_STAGNANT_PROFIT", "blocker": None})
    return result
