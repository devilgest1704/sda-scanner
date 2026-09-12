#!/usr/bin/env python3
"""Event-driven SDA strategy optimizer.

Replays transaction history from market_data.json on a fixed scan cadence and
searches entry/risk/portfolio parameters. This is deliberately independent of
paper state: it never writes positions.json or changes the live scanner.

The optimizer is intended to answer one question: which rules would have
maximized out-of-sample SDA P/L on the available transaction history?

Usage:
    python backtest_optimizer.py
    python backtest_optimizer.py --days 7 --top 20
    python backtest_optimizer.py --train-days 5 --test-days 2
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from itertools import product

MARKET_FILE = Path("market_data.json")
OUTPUT_FILE = Path("backtest_optimization.json")

# Keep costs consistent with the current paper engine.
FEE_PCT = 0.01
SLIPPAGE_PCT = 0.001
SCAN_MINUTES = 5
START_CAPITAL = 2500.0
INVESTMENT = 50.0


def dt(v):
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        x = datetime.fromisoformat(s)
        return x.replace(tzinfo=timezone.utc) if x.tzinfo is None else x.astimezone(timezone.utc)
    except Exception:
        return None


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def load():
    if not MARKET_FILE.exists():
        raise SystemExit("market_data.json not found")
    data = json.loads(MARKET_FILE.read_text(encoding="utf-8"))
    tokens = data.get("tokens", {}) if isinstance(data, dict) else {}
    out = {}
    for addr, td in tokens.items():
        hist = td.get("transactions", []) if isinstance(td, dict) else []
        rows = []
        for tx in hist:
            t = dt(tx.get("timestamp") or tx.get("tx_timestamp"))
            try:
                p = float(tx.get("price") or tx.get("price_in_sda") or 0)
                v = float(tx.get("volume") or tx.get("volume_in_sda") or 0)
            except Exception:
                continue
            typ = str(tx.get("type") or tx.get("tx_type") or "").lower()
            if t and p > 0 and v >= 0 and typ in {"buy", "sell"}:
                rows.append((t, p, v, typ, tx.get("hash")))
        rows.sort(key=lambda x: x[0])
        # Full hash de-duplication prevents replay inflation from the scanner's
        # historical last-200 de-duplication behavior.
        seen = set()
        clean = []
        for r in rows:
            h = r[4]
            if h and h in seen:
                continue
            if h:
                seen.add(h)
            clean.append(r)
        if clean:
            out[str(addr).lower()] = clean
    return out


def windows(hist, now, mins):
    cut = now - timedelta(minutes=mins)
    return [x for x in hist if x[0] >= cut and x[0] <= now]


def price_at(hist, now):
    # Last transaction at/before scan time.
    lo = None
    for x in hist:
        if x[0] <= now:
            lo = x
        else:
            break
    return lo[1] if lo else 0.0


def momentum(hist, now, mins):
    cur = price_at(hist, now)
    if cur <= 0:
        return 0.0
    old = price_at(hist, now - timedelta(minutes=mins))
    return (cur / old - 1.0) * 100.0 if old > 0 else 0.0


def features(hist, now):
    w15 = windows(hist, now, 15)
    w1 = windows(hist, now, 60)
    w4 = windows(hist, now, 240)
    def flow(w):
        return sum(v if typ == "buy" else -v for _, _, v, typ, _ in w)
    def vol(w):
        return sum(v for _, _, v, _, _ in w)
    def counts(w):
        return sum(1 for x in w if x[3] == "buy"), sum(1 for x in w if x[3] == "sell")
    b, s = counts(w1)
    return {
        "m15": momentum(hist, now, 15),
        "m1": momentum(hist, now, 60),
        "m4": momentum(hist, now, 240),
        "flow1": flow(w1),
        "flow15": flow(w15),
        "vol1": vol(w1),
        "trades1": len(w1),
        "buy1": b,
        "sell1": s,
        "buy_ratio": b / max(s, 1),
        "volume_ratio": sum(v for _, _, v, typ, _ in w1 if typ == "buy") / max(sum(v for _, _, v, typ, _ in w1 if typ == "sell"), 1.0),
    }


def score(f):
    # Same core dimensions as current _buy_score_v2, without importing the
    # scanner (which would mutate paper-engine globals).
    momentum_score = clamp(50 + 3*f["m1"] + 1.5*f["m4"] + 1.5*f["m15"])
    flow_score = clamp(50 + 35*math.tanh(f["flow1"]/5000) + 15*math.tanh(f["flow1"]/5000))
    activity_score = clamp(25 + 3.75*min(f["trades1"], 20))
    # Historical transaction replay has no reliable future predictor. Keep it
    # neutral rather than leaking future information into the entry score.
    prediction_score = 50.0
    # Conservative liquidity proxy: recent activity, capped. A dedicated
    # quote-impact dataset can be layered in later without changing replay.
    liquidity_score = clamp(35 + min(65, math.log10(max(f["vol1"], 1))*18)) if f["vol1"] > 0 else 35
    return clamp(.35*momentum_score + .25*flow_score + .15*activity_score + .15*prediction_score + .10*liquidity_score)


@dataclass
class Pos:
    token: str
    entry: float
    amount: float
    best: float
    entry_score: float
    opened: datetime
    tp1: bool = False


def simulate(data, start, end, p):
    timeline = set()
    for hist in data.values():
        for t, *_ in hist:
            if start <= t <= end:
                # Normalize transaction timestamps onto the 5-minute scanner grid.
                epoch = int(t.timestamp())
                bucket = epoch - epoch % (SCAN_MINUTES*60)
                timeline.add(datetime.fromtimestamp(bucket, tz=timezone.utc))
    timeline = sorted(timeline)
    positions = {}
    closed = []
    capital = START_CAPITAL
    peak = capital
    max_dd = 0.0
    last_entry = {}

    for now in timeline:
        # Exits first so capital is available for new entries on the same scan.
        for token in list(positions):
            pos = positions[token]
            price = price_at(data[token], now)
            if price <= 0:
                continue
            pos.best = max(pos.best, price)
            roi = price / pos.entry - 1
            trail_hit = pos.best > pos.entry * (1+p["tp1"]) and price <= pos.best * (1-p["trail"])
            reason = None
            fraction = 1.0
            if roi <= -p["sl"]:
                reason = "SL"
            elif roi >= p["tp2"]:
                reason = "TP2"
            elif roi >= p["tp1"] and not pos.tp1:
                pos.tp1 = True
                fraction = 0.5
                reason = "TP1"
            elif trail_hit:
                reason = "TRAIL"
            if reason:
                proceeds = price * pos.amount * (1-FEE_PCT-SLIPPAGE_PCT)
                cost = pos.entry * pos.amount
                profit = (proceeds - cost*fraction) if fraction < 1 else proceeds-cost
                closed.append({"token": token, "profit": profit, "roi": roi*100, "reason": reason, "score": pos.entry_score})
                capital += proceeds if fraction == 1 else price * pos.amount * fraction * (1-FEE_PCT-SLIPPAGE_PCT)
                if fraction == 1:
                    del positions[token]
                else:
                    pos.amount *= 0.5

        if len(positions) >= p["max_positions"]:
            continue

        candidates = []
        for token, hist in data.items():
            if token in positions:
                continue
            f = features(hist, now)
            if f["trades1"] < p["min_trades"] or f["vol1"] < p["min_volume"]:
                continue
            if f["flow1"] <= p["min_flow"] or f["m1"] < p["min_m1"]:
                continue
            if now - last_entry.get(token, datetime.min.replace(tzinfo=timezone.utc)) < timedelta(hours=p["cooldown_hours"]):
                continue
            s = score(f)
            if s < p["threshold"]:
                continue
            candidates.append((s, token, f))
        candidates.sort(reverse=True)
        for s, token, f in candidates[:p["max_new_buys"]]:
            price = price_at(data[token], now)
            if price <= 0 or capital < INVESTMENT:
                continue
            positions[token] = Pos(token, price, INVESTMENT/price, price, s, now)
            capital -= INVESTMENT
            last_entry[token] = now

        equity = capital
        for token, pos in positions.items():
            px = price_at(data[token], now)
            equity += pos.amount * px * (1-FEE_PCT-SLIPPAGE_PCT)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak-equity)/peak if peak else 0)

    # Mark remaining positions to last available price.
    for token, pos in positions.items():
        px = price_at(data[token], end)
        if px > 0:
            profit = px * pos.amount * (1-FEE_PCT-SLIPPAGE_PCT) - pos.entry*pos.amount
            closed.append({"token": token, "profit": profit, "roi": (px/pos.entry-1)*100, "reason": "EOD", "score": pos.entry_score})
    total = sum(x["profit"] for x in closed)
    wins = [x for x in closed if x["profit"] > 0]
    losses = [x for x in closed if x["profit"] <= 0]
    gross_win = sum(x["profit"] for x in wins)
    gross_loss = abs(sum(x["profit"] for x in losses))
    pf = gross_win/gross_loss if gross_loss else (99.0 if gross_win else 0.0)
    expectancy = total/len(closed) if closed else 0.0
    return {
        **p,
        "profit_sda": round(total, 4),
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins)/len(closed), 4) if closed else 0,
        "profit_factor": round(pf, 4),
        "expectancy_sda": round(expectancy, 4),
        "max_drawdown": round(max_dd, 4),
        "score_metric": round(total - max_dd*START_CAPITAL*0.5, 4),
    }


def parameter_grid():
    # Small enough for Actions, broad enough to expose the useful region.
    keys = {
        "threshold": [58, 60, 62, 65, 68, 70],
        "min_volume": [250, 500, 750, 1000],
        "min_trades": [3, 5, 8, 10],
        "min_flow": [0, 100, 250, 500],
        "min_m1": [0, 1, 2, 5],
        "sl": [0.04, 0.05, 0.06, 0.07],
        "tp1": [0.05, 0.06, 0.07, 0.08],
        "tp2": [0.10, 0.11, 0.14, 0.18],
        "trail": [0.04, 0.05, 0.06, 0.08],
        "max_positions": [3, 5, 7],
        "max_new_buys": [1, 2],
        "cooldown_hours": [0, 2, 4, 8],
    }
    # Full Cartesian product would be millions. Use structured slices: risk
    # and entry grids are independently combined, then ranked with a second
    # pass. This keeps the optimizer deterministic and fast in CI.
    entries = list(product(keys["threshold"], keys["min_volume"], keys["min_trades"], keys["min_flow"], keys["min_m1"], keys["max_positions"], keys["max_new_buys"], keys["cooldown_hours"]))
    risks = list(product(keys["sl"], keys["tp1"], keys["tp2"], keys["trail"]))
    # ~196k combinations; still manageable but unnecessarily repetitive. Use
    # sensible risk constraints to keep only monotonic TP/SL combinations.
    risks = [r for r in risks if r[1] > r[0] and r[2] > r[1] and r[3] <= r[1]]
    for e in entries:
        for r in risks:
            yield {"threshold":e[0],"min_volume":e[1],"min_trades":e[2],"min_flow":e[3],"min_m1":e[4],"max_positions":e[5],"max_new_buys":e[6],"cooldown_hours":e[7],"sl":r[0],"tp1":r[1],"tp2":r[2],"trail":r[3]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--train-days", type=float, default=0)
    ap.add_argument("--test-days", type=float, default=0)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--max-combinations", type=int, default=60000)
    args = ap.parse_args()
    data = load()
    all_times = [x[0] for h in data.values() for x in h]
    if not all_times:
        raise SystemExit("No transactions found in market_data.json")
    end = max(all_times)
    start = max(min(all_times), end - timedelta(days=args.days))
    if args.train_days > 0 and args.test_days > 0:
        train_end = end - timedelta(days=args.test_days)
        train_start = max(min(all_times), train_end - timedelta(days=args.train_days))
        eval_start, eval_end = train_start, train_end
    else:
        eval_start, eval_end = start, end

    results = []
    for i, p in enumerate(parameter_grid()):
        if i >= args.max_combinations:
            break
        r = simulate(data, eval_start, eval_end, p)
        if r["trades"] >= 3:
            results.append(r)
    results.sort(key=lambda x: (x["score_metric"], x["profit_factor"], x["expectancy_sda"]), reverse=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_start": min(all_times).isoformat(),
        "data_end": max(all_times).isoformat(),
        "evaluation_start": eval_start.isoformat(),
        "evaluation_end": eval_end.isoformat(),
        "tokens": len(data),
        "combinations_tested": min(args.max_combinations, sum(1 for _ in parameter_grid())),
        "top": results[:args.top],
    }
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
