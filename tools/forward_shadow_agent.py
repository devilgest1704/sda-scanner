#!/usr/bin/env python3
"""Forward-only virtual paper challengers; no orders and no historical hindsight.

Every strategy gets the same timestamped candidate snapshots and subsequent
per-token market quotes. We keep separate simulated positions, cost and exit
journals for each strategy. This is research evidence, NOT executable fills.
"""
from __future__ import annotations
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("strategy_lab/forward_shadow_state.json")
SIZE_SDA = 50.0
FEE_AND_SLIPPAGE = 0.011  # each side; conservative 2.2% round-trip
STOP_PCT = -0.025
TAKE_PCT = 0.08
TRAIL_PCT = 0.035
MAX_HOURS = 4
MAX_POSITIONS = 4
COOLDOWN_HOURS = 6
MAX_QUOTE_GAP_MIN = 30
MAX_HISTORY = 5000
# Only entry-gate parameters actually supported by V30 runtime are varied.
POLICIES = {
    "baseline": {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 5},
    "quality": {"entry_score": 72, "quality": 56, "impulse": 8, "buy_ratio": 1.20, "trades": 12},
    "momentum": {"entry_score": 76, "quality": 58, "impulse": 10, "buy_ratio": 1.25, "trades": 14},
    "selective": {"entry_score": 80, "quality": 62, "impulse": 12, "buy_ratio": 1.35, "trades": 18},
    "score": {"entry_score": 84, "quality": 52, "impulse": 8, "buy_ratio": 1.10, "trades": 12},
    "quality_plus": {"entry_score": 68, "quality": 62, "impulse": 8, "buy_ratio": 1.15, "trades": 12},
    # Observational only: WATCH research is NOT a deployable Champion lane.
    "research_watch": {"entry_score": 68, "quality": 55, "impulse": 8, "buy_ratio": 1.20, "trades": 8},
    "research_watch_strict": {"entry_score": 76, "quality": 65, "impulse": 12, "buy_ratio": 1.45, "trades": 12},
}


def when(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def positive(value):
    n = finite(value)
    return n if n is not None and n > 0 else None


def default_state():
    return {
        "schema": 1, "paper_only": True, "method": "forward_observed_quote_shadow_simulation",
        "last_snapshot_at": None, "updated_at": None, "runs": 0,
        "strategies": {name: {"filters": filters.copy(), "positions": {},
                             "trades": [], "last_entry": {}}
                       for name, filters in POLICIES.items()},
    }


def load_state(path=STATE):
    if not path.exists():
        return default_state()
    # A malformed ledger is never silently reset. Preserve the file for audit.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("paper_only") is not True:
        raise ValueError("Invalid forward shadow ledger; refusing to overwrite")
    strategies = data.get("strategies")
    if not isinstance(strategies, dict):
        raise ValueError("Missing forward shadow strategies")
    for name in POLICIES:
        if name not in strategies:
            strategies[name] = default_state()["strategies"][name]
        st = strategies[name]
        if not isinstance(st, dict) or not isinstance(st.get("positions"), dict) or not isinstance(st.get("trades"), list):
            raise ValueError("Invalid forward shadow position/trade state")
    return data


def quote_map(market, now):
    # Only a valid per-token transaction timestamp can advance a virtual price.
    raw = market.get("tokens", {}) if isinstance(market, dict) else {}
    result = {}
    if not isinstance(raw, dict):
        return result
    for addr, token in raw.items():
        if not isinstance(token, dict):
            continue
        info = token.get("analysis", token)
        if not isinstance(info, dict):
            continue
        stamp = when(info.get("last_transaction") or token.get("last_transaction"))
        price = positive(info.get("price_in_sda"))
        if stamp and price and stamp <= now:
            result[str(addr).lower()] = (stamp, price)
    return result


def eligible(c, filters, research_watch=False):
    if not isinstance(c, dict):
        return False
    phase = c.get("phase")
    if research_watch:
        if phase != "WATCH":
            return False
    elif phase not in ("IGNITION", "CONFIRMATION"):
        return False
    age = finite(c.get("data_age_sec"))
    score = finite(c.get("score"))
    quality = finite(c.get("quality"))
    impulse = finite(c.get("snapshot_impulse") if research_watch else c.get("impulse"))
    trades = finite(c.get("trades_1h"))
    ratio = finite(c.get("buy_ratio"))
    volume = finite(c.get("volume_1h"))
    flow = finite(c.get("flow_1h"))
    m15 = finite(c.get("m15"))
    m1h = finite(c.get("m1h"))
    values = (age, score, quality, impulse, trades, ratio, volume, flow, m15, m1h)
    if any(v is None for v in values):
        return False
    if research_watch:
        # Collect error/missed-opportunity evidence with stricter data safety.
        # These WATCH experiments are never eligible for automatic promotion.
        if not (0 <= age <= 180 and 0 < m1h <= 8 and m15 >= 0.8
                and volume >= 600 and flow >= 250 and trades >= 8):
            return False
    else:
        if not (0 <= age <= 300 and 0 <= m1h <= 12):
            return False
        # Common hard V31 safety checks cannot be tuned away.
        if not (volume >= 500 and flow >= 150
                and m15 >= (1.5 if phase == "CONFIRMATION" else 0.8)
                and trades >= 12):
            return False
    return (score >= filters["entry_score"] and quality >= filters["quality"]
            and impulse >= filters["impulse"] and ratio >= filters["buy_ratio"]
            and trades >= filters["trades"])

def exit_profit(entry, price):
    # Notional paper net result after modelled entry/exit friction, in SDA.
    return SIZE_SDA * ((price * (1 - FEE_AND_SLIPPAGE)) /
                       (entry * (1 + FEE_AND_SLIPPAGE)) - 1)


def step(data, market, funnel):
    if not isinstance(data, dict) or not isinstance(funnel, dict):
        raise ValueError("Invalid inputs")
    ts = when(funnel.get("at"))
    market_ts = when(market.get("updated_at")) if isinstance(market, dict) else None
    previous = when(data.get("last_snapshot_at"))
    if ts is None or market_ts is None or abs((ts - market_ts).total_seconds()) > 600:
        raise ValueError("Candidate and market snapshots are not time-aligned")
    if previous and ts <= previous:
        return {"status": "already_processed", "at": funnel.get("at"), "opened": 0, "closed": 0}
    quotes = quote_map(market, market_ts)
    candidates = funnel.get("candidate_snapshots", [])
    if not isinstance(candidates, list):
        raise ValueError("Missing candidate snapshots")
    ranked = sorted((x for x in candidates if isinstance(x, dict)),
                    key=lambda x: finite(x.get("score")) or 0, reverse=True)
    opened = closed = 0
    for name, filters in POLICIES.items():
        strategy = data["strategies"][name]
        positions = strategy["positions"]
        trades = strategy["trades"]
        last_entry = strategy.setdefault("last_entry", {})
        # Process exits BEFORE new entries. A new entry never receives its own
        # opening quote as a future exit, and cannot use unseen future prices.
        for address, position in list(positions.items()):
            quote = quotes.get(address)
            last = when(position.get("last_quote_at"))
            start = when(position.get("opened_at"))
            if start is None or last is None:
                raise ValueError("Corrupt forward position time")
            if quote is None or quote[0] <= last or quote[0] <= start:
                # Never allow stale/unquoted token positions to permanently
                # occupy the research portfolio. This is NOT an observed fill.
                if (ts - start).total_seconds() >= 8*3600:
                    trades.append({
                        "strategy": name, "address": address,
                        "opened_at": position["opened_at"], "closed_at": ts.isoformat(),
                        "entry_price_sda": position["entry_price_sda"],
                        "exit_price_sda": None, "pnl_sda": -SIZE_SDA,
                        "roi_pct": -100.0, "reason": "STALE_TIMEOUT",
                        "quote_gap_minutes": round((ts-last).total_seconds()/60,2),
                        "valid_for_learning": False, "notional_sda": SIZE_SDA,
                        "model": "conservative_risk_writeoff_no_observed_fill"
                    })
                    del positions[address]
                    closed += 1
                continue
            stamp, price = quote
            gap = (stamp - last).total_seconds() / 60
            duration = (stamp - start).total_seconds() / 3600
            entry = positive(position.get("entry_price_sda"))
            if entry is None:
                raise ValueError("Corrupted forward shadow entry price")
            position["last_quote_at"] = stamp.isoformat()
            position["peak_price_sda"] = max(price, positive(position.get("peak_price_sda")) or entry)
            move = price / entry - 1
            trailing = price / position["peak_price_sda"] - 1
            reason = ("STOP" if move <= STOP_PCT else
                      "TAKE" if move >= TAKE_PCT else
                      "TRAIL" if duration >= 0.25 and trailing <= -TRAIL_PCT else
                      "TIME" if duration >= MAX_HOURS else None)
            if not reason:
                continue
            net = round(exit_profit(entry, price), 6)
            # Gapped or delayed observed prices are recorded but excluded from
            # trustworthy model selection.
            valid = (gap <= MAX_QUOTE_GAP_MIN and duration <= MAX_HOURS + 0.5
                     and 0.2 <= price/entry <= 4.0)
            trades.append({
                "strategy": name, "address": address,
                "opened_at": position["opened_at"], "closed_at": stamp.isoformat(),
                "entry_price_sda": entry, "exit_price_sda": price,
                "pnl_sda": net, "roi_pct": round(net / SIZE_SDA * 100, 5),
                "reason": reason, "quote_gap_minutes": round(gap, 2),
                "valid_for_learning": valid, "notional_sda": SIZE_SDA,
                "model": "observed_quote_paper_not_executable_fill"
            })
            del positions[address]
            closed += 1
        # Refractory period prevents a volatile token from producing an
        # artificial stream of repeated simulated entries.
        if len(positions) < MAX_POSITIONS:
            for c in ranked:
                address = str(c.get("address") or "").lower()
                if not address or address in positions or not eligible(c, filters, name.startswith('research_watch')):
                    continue
                quote = quotes.get(address)
                entry = positive(c.get("price_sda"))
                if quote is None or entry is None:
                    continue
                stamp, quote_price = quote
                # Avoid treating a stale or unrelated quote as an execution.
                if stamp > ts or (ts - stamp).total_seconds() > 300:
                    continue
                # Candidate and market prices are contemporaneous.
                if abs(quote_price / entry - 1) > 0.015:
                    continue
                previous_entry = when(last_entry.get(address))
                if previous_entry and (ts - previous_entry).total_seconds() < COOLDOWN_HOURS * 3600:
                    continue
                positions[address] = {
                    "opened_at": ts.isoformat(), "entry_price_sda": entry,
                    "last_quote_at": stamp.isoformat(), "peak_price_sda": entry
                }
                last_entry[address] = ts.isoformat()
                opened += 1
                break  # max one entry per strategy per scanner cycle
        if len(trades) > MAX_HISTORY:
            del trades[:-MAX_HISTORY]
        # The last entry timestamps can be pruned without changing positions.
        for key, value in list(last_entry.items()):
            d = when(value)
            if d and (ts - d).total_seconds() > 86400:
                del last_entry[key]
    data["last_snapshot_at"] = ts.isoformat()
    data["updated_at"] = ts.isoformat()
    data["runs"] = int(data.get("runs", 0)) + 1
    return {"status": "processed", "at": ts.isoformat(), "opened": opened, "closed": closed,
            "quotes": len(quotes), "policies": len(POLICIES)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--market", default="market_analysis.json")
    ap.add_argument("--funnel", default="v30_execution_state.json")
    args = ap.parse_args()
    state_path = Path(args.state)
    state = load_state(state_path)
    market = json.loads(Path(args.market).read_text(encoding="utf-8"))
    execution = json.loads(Path(args.funnel).read_text(encoding="utf-8"))
    funnels = execution.get("funnels", [])
    if not isinstance(funnels, list) or not funnels:
        raise ValueError("No V30 funnel snapshots")
    outcome = step(state, market, funnels[-1])
    if outcome["status"] == "processed":
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(state_path)
    print("FORWARD SHADOW AGENT: " + json.dumps(outcome))


if __name__ == "__main__":
    main()
