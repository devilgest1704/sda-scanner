"""REAL-only stagnant-profit policy and bounded position observations."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise

REASON = "MAX_HOLD_STAGNANT_PROFIT"
MIN_HOLD = 72 * 3600
STAGNATION_WINDOW = 24 * 3600
MIN_IMPROVEMENT = Decimal(1)
PRICE_MAX_AGE = 300
MAX_OBSERVATION_GAP = 30 * 60
HISTORY_KEY = "max_hold_history"
SHADOW_KEY = "max_hold_shadow"


def number(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def timestamp(value: object) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.timestamp() if stamp.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def fresh(stamp: float | None, now: float, limit: int = PRICE_MAX_AGE) -> bool:
    return stamp is not None and 0 <= now - stamp <= limit


def observations(position: dict, now: float, observed: float | None,
                 price: Decimal, pnl_pct: Decimal) -> list[list]:
    opened = timestamp(position.get("opened_at"))
    if opened is None:
        return []
    start = max(opened, now - STAGNATION_WINDOW)
    buckets: dict[int, tuple[list, list]] = {}
    points = position.get(HISTORY_KEY)
    points = points if isinstance(points, list) else []
    if fresh(observed, now) and price.is_finite() and price > 0 and pnl_pct.is_finite():
        points = [*points, [observed, str(price), str(pnl_pct)]]
    for point in points:
        if not isinstance(point, list) or len(point) != 3:
            continue
        stamp, raw_price, raw_pnl = point
        point_price, point_pnl = number(raw_price), number(raw_pnl)
        if (not isinstance(stamp, (int, float)) or not start <= stamp <= now
                or point_price is None or point_price <= 0 or point_pnl is None):
            continue
        bucket = int(stamp // 60)
        candidate = [stamp, str(point_price), str(point_pnl)]
        first, peak = buckets.get(bucket, (candidate, candidate))
        if stamp < first[0]:
            first = candidate
        if (point_pnl, -stamp) > (Decimal(peak[2]), -peak[0]):
            peak = candidate
        if first[0] == peak[0]:
            first = peak
        buckets[bucket] = first, peak
    result = []
    for key in sorted(buckets):
        first, peak = buckets[key]
        result.append(first)
        if first != peak:
            result.append(peak)
    return result


def history_summary(points: list[list]) -> dict:
    covered = sum(right[0] - left[0] for left, right in pairwise(points)
                  if right[0] - left[0] <= MAX_OBSERVATION_GAP)
    coverage = covered / STAGNATION_WINDOW
    summary = {"history_coverage": coverage, "pnl_start_pct": None,
               "pnl_max_pct": None, "improvement_pp": None}
    if len(points) < 2:
        return summary
    start = Decimal(points[0][2])
    peak = max(Decimal(point[2]) for point in points)
    summary.update(pnl_start_pct=str(start), pnl_max_pct=str(peak), improvement_pp=str(peak - start))
    return summary


def eligibility(position: dict, now: float, observed: float | None, pnl_pct: Decimal,
                points: list[list], summary: dict) -> str | None:
    opened = timestamp(position.get("opened_at"))
    if not management_enabled(position):
        return "SELL_MANAGEMENT_DISABLED"
    if position.get("profit_partial_done"):
        return "PARTIAL_ALREADY_DONE"
    if position.get("trailing_armed"):
        return "TRAILING_ARMED"
    if not fresh(observed, now):
        return "STALE_PRICE"
    if not pnl_pct.is_finite() or pnl_pct <= 0:
        return "NON_POSITIVE_PNL"
    if opened is None or now - opened < MIN_HOLD:
        return "MIN_HOLD_NOT_MET"
    if (summary["history_coverage"] < 0.75 or not points
            or points[0][0] > now - STAGNATION_WINDOW + MAX_OBSERVATION_GAP
            or points[-1][0] < now - PRICE_MAX_AGE):
        return "INSUFFICIENT_HISTORY"
    if Decimal(summary["improvement_pp"]) >= MIN_IMPROVEMENT:
        return "PNL_IMPROVING"
    return None


def management_enabled(position: dict) -> bool:
    return position.get("sellManagementEnabled", bool(position.get("buy_tx"))) is True


def trend(snapshot: dict, token: str, now: float) -> tuple[dict, str | None]:
    item = _mapping(_mapping(snapshot.get("tokens")).get(token))
    momentum = _mapping(item.get("momentum"))
    flows = _mapping(item.get("flow"))
    m15, hour, four = (number(momentum.get(key)) for key in ("15m_pct", "1h_pct", "4h_pct"))
    flow15, flow_hour, flow_four = (number(_mapping(flows.get(key)).get("net_flow"))
                                    for key in ("15m", "1h", "4h"))
    technical = _mapping(_mapping(item.get("technical")).get("1h_completed"))
    macd = _mapping(technical.get("macd"))
    histogram = number(macd.get("histogram_pct"))
    rising = macd.get("histogram_rising") is True
    macd_state = "UNAVAILABLE"
    macd_stale = False
    if technical.get("available") is True:
        macd_stale = histogram is None or not fresh(timestamp(technical.get("last_completed_at")), now, 3900)
        if not macd_stale and histogram is not None:
            macd_state = "BULLISH" if histogram > 0 else "BEARISH" if histogram < 0 else "FLAT"
        else:
            macd_state = "STALE"
    row = {"momentum_15m_pct": _string(m15), "momentum_1h_pct": _string(hour),
           "momentum_4h_pct": _string(four), "flow_15m_sda": _string(flow15),
           "flow_1h_sda": _string(flow_hour), "flow_4h_sda": _string(flow_four),
           "macd_state": macd_state, "macd_histogram_pct": _string(histogram),
           "macd_histogram_rising": rising, "trend": "UNKNOWN"}
    if (macd_stale or not fresh(timestamp(snapshot.get("updated_at")), now)
            or not fresh(timestamp(item.get("last_transaction")), now)):
        return row, "STALE_TREND"
    if (m15 is None or hour is None or four is None or flow15 is None
            or flow_hour is None or flow_four is None
            or any((number(_mapping(flows.get(key)).get("transactions")) or 0) <= 0
                   for key in ("15m", "1h", "4h"))):
        return row, "INSUFFICIENT_TREND"
    if (hour >= 1 or four >= 1 or (hour > 0 and four > 0)
            or (hour > 0 and flow_hour > 0) or (four > 0 and flow_four > 0)):
        return {**row, "trend": "BULLISH"}, "POSITIVE_MOMENTUM"
    if (m15 >= 1 or (m15 > 0 and flow15 > 0)
            or (macd_state == "BULLISH" and rising)
            or (flow_hour > 0 and flow_four > 0)):
        return {**row, "trend": "RECOVERY"}, "BULLISH_RECOVERY"
    return {**row, "trend": "FLAT" if min(hour, four, m15) >= -1 else "BEARISH"}, None


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}