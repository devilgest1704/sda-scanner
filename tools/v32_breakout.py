"""V32 Breakout + Volume + Trend: observation-only SDA research signal.

Calculate strictly at the supplied scanner snapshot, from transactions the
scanner had already received. Never create a BUY or assume execution at an
unobserved quote. Parameters are fixed before future outcome observations.
"""
from datetime import datetime, timedelta, timezone
import math

BASE_START_MIN = 150
BASE_END_MIN = 30
VOLUME_COMPARE_MIN = 60
RECENT_MIN = 15
QUOTE_MAX_AGE_SEC = 300
MIN_BASE_TRADES = 10
MIN_BASE_SPAN_MIN = 35
MAX_BASE_WIDTH_PCT = 8.0
MIN_BREAKOUT_PCT = .5
MAX_BREAKOUT_PCT = 8.0
MIN_RECENT_TRADES = 5
MIN_RECENT_BUYS = 3
MIN_RECENT_VOLUME_SDA = 250.0
MIN_RECENT_NET_FLOW_SDA = 150.0
MIN_BUY_RATIO = 1.35
MIN_VOLUME_MULTIPLE = 1.5
MIN_PREVIOUS_VOLUME_SDA = 60.0
MIN_RECENT_PRICE_MOMENTUM_PCT = .75
MAX_RECENT_PRICE_MOMENTUM_PCT = 12.0
MIN_PEAK_RETENTION = .97


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        v = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return v.astimezone(timezone.utc) if v.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def nonnegative(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def analyze_breakout(history, as_of):
    """Return explainable eligibility from one point-in-time observed history.

    The consolidation high ends 30m *before* the candidate quote, preventing
    today's breakout price from leaking into its resistance estimate.
    A separate 15m window validates rising price, transaction activity, buy
    pressure and real volume acceleration. Missing evidence fails closed.

    This is not a prediction, not a counterfactual backtest and not a fill.
    """
    base = {
        "version": "V32_BREAKOUT_OBSERVATION_1",
        "mode": "RESEARCH_ONLY",
        "available": False,
        "eligible": False,
        "reason": "invalid_snapshot",
        "rejection_reasons": ["invalid_snapshot"],
        "observed_at": as_of.astimezone(timezone.utc).isoformat()
            if isinstance(as_of, datetime) and as_of.tzinfo is not None else None,
        "reference_price_sda": None,
        "last_transaction_at": None,
        "quote_age_seconds": None,
        "base_trades": 0,
        "base_span_minutes": None,
        "base_range_pct": None,
        "resistance_sda": None,
        "breakout_pct": None,
        "recent_15m_price_change_pct": None,
        "recent_15m_volume_sda": None,
        "recent_15m_net_flow_sda": None,
        "recent_15m_buy_ratio": None,
        "volume_multiple": None,
        "recent_15m_swaps": 0,
        "recent_15m_buys": 0,
        "last_vs_recent_peak_pct": None,
    }
    if not isinstance(history, list) or not isinstance(as_of, datetime) or as_of.tzinfo is None:
        return base
    as_of = as_of.astimezone(timezone.utc)
    base_start = as_of - timedelta(minutes=BASE_START_MIN)
    base_end = as_of - timedelta(minutes=BASE_END_MIN)
    prev_start = as_of - timedelta(minutes=VOLUME_COMPARE_MIN)
    recent_start = as_of - timedelta(minutes=RECENT_MIN)
    observations = []
    for row in history:
        if not isinstance(row, dict):
            continue
        at = timestamp(row.get("timestamp"))
        price = positive(row.get("price"))
        volume = nonnegative(row.get("volume"))
        side = str(row.get("type") or "").lower()
        if (at is None or at > as_of or at < base_start
                or price is None or volume is None or side not in ("buy", "sell")):
            continue
        observations.append((at, price, volume, side))
    observations.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    old = [x for x in observations if base_start <= x[0] < base_end]
    previous = [x for x in observations if prev_start <= x[0] < recent_start]
    recent = [x for x in observations if recent_start <= x[0] <= as_of]
    latest = observations[-1] if observations else None
    if latest is None:
        return {**base, "reason": "no_observed_transactions",
                "rejection_reasons": ["no_observed_transactions"]}
    last_at, price, _, _ = latest
    quote_age = (as_of - last_at).total_seconds()
    update = {
        **base,
        "reference_price_sda": price,
        "last_transaction_at": last_at.isoformat(),
        "quote_age_seconds": round(quote_age, 2),
        "base_trades": len(old),
        "recent_15m_swaps": len(recent),
    }
    reasons = []
    if quote_age > QUOTE_MAX_AGE_SEC:
        reasons.append("stale_quote")
    if len(old) < MIN_BASE_TRADES:
        reasons.append("insufficient_base_trades")
    if not recent or len(recent) < MIN_RECENT_TRADES:
        reasons.append("insufficient_recent_trades")
    if not old:
        return {**update, "reason": reasons[0], "rejection_reasons": reasons}
    span = (old[-1][0] - old[0][0]).total_seconds() / 60
    low = min(x[1] for x in old)
    high = max(x[1] for x in old)
    width = (high/low - 1)*100
    breakout = (price/high - 1)*100
    update.update({
        "base_span_minutes": round(span, 2),
        "base_range_pct": round(width, 4),
        "resistance_sda": high,
        "breakout_pct": round(breakout, 4),
    })
    if span < MIN_BASE_SPAN_MIN:
        reasons.append("insufficient_base_time_coverage")
    if width > MAX_BASE_WIDTH_PCT:
        reasons.append("consolidation_too_wide")
    if not MIN_BREAKOUT_PCT <= breakout <= MAX_BREAKOUT_PCT:
        reasons.append("outside_early_breakout_zone")
    if not recent:
        return {**update, "reason": reasons[0], "rejection_reasons": reasons}
    buy_vol = sum(x[2] for x in recent if x[3] == "buy")
    sell_vol = sum(x[2] for x in recent if x[3] == "sell")
    current_vol = buy_vol + sell_vol
    net_flow = buy_vol - sell_vol
    ratio = buy_vol / max(sell_vol, 1)
    buy_count = sum(x[3] == "buy" for x in recent)
    prev_vol = sum(x[2] for x in previous)
    # 45m volume divided by three gives comparable 15m activity.
    vol_multiple = current_vol/(prev_vol/3) if prev_vol > 0 else None
    ref = next((x[1] for x in reversed(observations)
                if x[0] < recent_start), None)
    if ref is None:
        ref = recent[0][1]
    recent_momentum = (price/ref - 1)*100
    recent_peak = max(x[1] for x in recent)
    peak_retention = 100*price/recent_peak
    update.update({
        "recent_15m_volume_sda": round(current_vol, 3),
        "recent_15m_net_flow_sda": round(net_flow, 3),
        "recent_15m_buy_ratio": round(ratio, 4),
        "recent_15m_swaps": len(recent),
        "recent_15m_buys": buy_count,
        "volume_multiple": round(vol_multiple, 3) if vol_multiple is not None else None,
        "recent_15m_price_change_pct": round(recent_momentum, 4),
        "last_vs_recent_peak_pct": round(peak_retention, 4),
    })
    if buy_count < MIN_RECENT_BUYS:
        reasons.append("insufficient_buy_transactions")
    if current_vol < MIN_RECENT_VOLUME_SDA:
        reasons.append("insufficient_recent_volume")
    if net_flow < MIN_RECENT_NET_FLOW_SDA or ratio < MIN_BUY_RATIO:
        reasons.append("weak_buying_pressure")
    if prev_vol < MIN_PREVIOUS_VOLUME_SDA or vol_multiple is None or vol_multiple < MIN_VOLUME_MULTIPLE:
        reasons.append("volume_not_accelerating")
    if not MIN_RECENT_PRICE_MOMENTUM_PCT <= recent_momentum <= MAX_RECENT_PRICE_MOMENTUM_PCT:
        reasons.append("recent_price_momentum_not_confirmed")
    if price/recent_peak < MIN_PEAK_RETENTION:
        reasons.append("already_reversing_from_peak")
    update["available"] = len(old) >= MIN_BASE_TRADES and span >= MIN_BASE_SPAN_MIN
    update["eligible"] = not reasons
    update["reason"] = reasons[0] if reasons else "confirmed_early_breakout"
    update["rejection_reasons"] = reasons
    return update


def research_signal_from_analysis(analysis, scanner_at, market_at):
    """Join breakout evidence to a same-scan snapshot, fail closed on staleness."""
    if not isinstance(analysis, dict):
        return None
    raw = (analysis.get("technical") or {}).get("breakout_volume_trend")
    if not isinstance(raw, dict) or not isinstance(scanner_at, datetime) or not isinstance(market_at, datetime):
        return None
    measured = timestamp(raw.get("observed_at"))
    last_tx = timestamp(raw.get("last_transaction_at"))
    age = nonnegative(raw.get("quote_age_seconds"))
    ref_price = positive(raw.get("reference_price_sda"))
    current_price = positive(analysis.get("price_in_sda"))
    if (measured is None or last_tx is None or scanner_at.tzinfo is None
            or market_at.tzinfo is None or measured > market_at
            or market_at > scanner_at
            or not 0 <= (scanner_at-measured).total_seconds() <= QUOTE_MAX_AGE_SEC
            or not 0 <= (scanner_at-last_tx).total_seconds() <= QUOTE_MAX_AGE_SEC
            or last_tx > measured or age is None
            or abs(age-(measured-last_tx).total_seconds()) > 10
            or ref_price is None or current_price is None
            or abs(ref_price/current_price-1) > .015
            or not isinstance(raw.get("eligible"), bool)
            or not isinstance(raw.get("rejection_reasons"), list)):
        return None
    fields = (
        "version", "mode", "available", "eligible", "reason", "observed_at",
        "last_transaction_at", "reference_price_sda", "quote_age_seconds",
        "base_trades", "base_span_minutes", "base_range_pct",
        "resistance_sda", "breakout_pct", "recent_15m_price_change_pct",
        "recent_15m_volume_sda", "recent_15m_net_flow_sda",
        "recent_15m_buy_ratio", "volume_multiple", "recent_15m_swaps",
        "recent_15m_buys", "last_vs_recent_peak_pct", "rejection_reasons",
    )
    return {key: raw.get(key) for key in fields}



SPARSE_VOLUME_ONLY_REJECTIONS = frozenset((
    "insufficient_recent_trades", "insufficient_buy_transactions",
))


def sparse_volume_research_eligible(row, filters):
    """Forward-only hypothesis: large early buy flow in just 2-4 swaps.

    Reject stale observations, late 4h pumps and EVERY breakout rejection
    except a sparse transaction count. Research-only: NEVER a V30 PAPER BUY.
    This specifically tests whether the normal 5-swap minimum hides genuine
    early moves, rather than weakening the live/paper risk circuit.
    """
    if not isinstance(row, dict) or row.get("phase") not in (
            "WATCH", "IGNITION", "CONFIRMATION"):
        return False
    signal = row.get("v32_breakout")
    if not isinstance(signal, dict) or signal.get("mode") != "RESEARCH_ONLY":
        return False
    reasons = signal.get("rejection_reasons")
    if (signal.get("available") is not True
            or signal.get("eligible") is not False
            or not isinstance(reasons, list)
            or not reasons
            or not set(reasons).issubset(SPARSE_VOLUME_ONLY_REJECTIONS)):
        return False

    def number(source, key):
        v = source.get(key)
        if isinstance(v, bool):
            return None
        try:
            n = float(v)
            return n if math.isfinite(n) else None
        except (TypeError, ValueError, OverflowError):
            return None

    keys = (
        "score", "quality", "snapshot_impulse", "delta_impulse",
        "data_age_sec", "m15", "m1h", "m4h", "trades_1h",
        "buy_ratio", "volume_1h", "flow_1h", "flow_15m", "price_sda",
    )
    c = {key: number(row, key) for key in keys}
    evidence = (
        "base_trades", "base_span_minutes", "base_range_pct",
        "breakout_pct", "recent_15m_swaps", "recent_15m_buys",
        "recent_15m_volume_sda", "recent_15m_net_flow_sda",
        "recent_15m_buy_ratio", "volume_multiple",
        "recent_15m_price_change_pct", "last_vs_recent_peak_pct",
        "reference_price_sda",
    )
    s = {key: number(signal, key) for key in evidence}
    if any(x is None for x in (*c.values(), *s.values())):
        return False
    if not (
            0 <= c["data_age_sec"] <= 180
            and c["score"] >= filters["entry_score"]
            and c["quality"] >= filters["quality"]
            and max(c["snapshot_impulse"], c["delta_impulse"]) >= filters["impulse"]
            and 0 < c["m1h"] <= 8 and 0.75 <= c["m15"] <= 8
            and -5 <= c["m4h"] <= 30  # never chase an already +128% pump
            and c["trades_1h"] >= filters["trades"]
            and c["buy_ratio"] >= filters["buy_ratio"]
            and c["volume_1h"] >= 650 and c["flow_1h"] >= 300
            and c["flow_15m"] >= 350 and c["price_sda"] > 0
            and s["base_trades"] >= MIN_BASE_TRADES
            and s["base_span_minutes"] >= MIN_BASE_SPAN_MIN
            and s["base_range_pct"] <= MAX_BASE_WIDTH_PCT
            and 0.5 <= s["breakout_pct"] <= 6.0
            and 2 <= s["recent_15m_swaps"] <= 4
            and s["recent_15m_buys"] >= 2
            and s["recent_15m_volume_sda"] >= 500
            and s["recent_15m_net_flow_sda"] >= 350
            and s["recent_15m_buy_ratio"] >= 1.5
            and s["volume_multiple"] >= 2.0
            and 0.75 <= s["recent_15m_price_change_pct"] <= 8.0
            and s["last_vs_recent_peak_pct"] >= 97.5
            and s["reference_price_sda"] > 0
            and abs(c["price_sda"] / s["reference_price_sda"] - 1) <= .015):
        return False
    return True


def breakout_research_eligible(row, filters):
    """Early breakout research may use WATCH, unlike production V30 gates.

    This is a separate, non-promotable research gate, not an execution order.
    The signal itself uses only observed transactions and includes additional
    consolidation, volume, buy-pressure and price-strength requirements.
    """
    if not isinstance(row, dict) or row.get("phase") not in ("WATCH", "IGNITION", "CONFIRMATION"):
        return False
    signal = row.get("v32_breakout")
    if not isinstance(signal, dict) or signal.get("eligible") is not True:
        return False
    def numeric(k):
        x = row.get(k)
        if isinstance(x, bool):
            return None
        try:
            v = float(x)
            return v if math.isfinite(v) else None
        except (ValueError, TypeError, OverflowError):
            return None
    fields = {k: numeric(k) for k in (
        "score", "quality", "impulse", "snapshot_impulse", "data_age_sec",
        "m15", "m1h", "trades_1h", "buy_ratio", "volume_1h",
        "flow_1h", "price_sda",
    )}
    if any(v is None for v in fields.values()):
        return False
    age = fields["data_age_sec"]
    return (0 <= age <= QUOTE_MAX_AGE_SEC
            and fields["score"] >= filters["entry_score"]
            and fields["quality"] >= filters["quality"]
            and max(fields["impulse"], fields["snapshot_impulse"]) >= filters["impulse"]
            and fields["trades_1h"] >= max(8, filters["trades"])
            and fields["buy_ratio"] >= filters["buy_ratio"]
            and fields["volume_1h"] >= 350
            and fields["flow_1h"] >= 150
            and .25 <= fields["m15"] <= 8
            and 0 <= fields["m1h"] <= 12
            and positive(fields["price_sda"]) is not None
            and positive(signal.get("reference_price_sda")) is not None
            and abs(fields["price_sda"]/signal["reference_price_sda"]-1) <= .015)
