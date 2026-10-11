#!/usr/bin/env python3
"""V31 research: observed trade losses and *earlier-only* exit hypotheses.

No orders, no Champion changes, no optimizer promotion, no extrapolated quotes.
Candidate paths use observed V30 scanner samples with recorded transaction age.
A hypothetical strategy may EXIT EARLIER than the incumbent, never later: we
have no prices past the real exit. All comparisons use identical eligible trades,
same 1% fee / 0.1% slip per exit and 1% entry fee, and 50 SDA notional.
Selection uses TRAIN only; chronological HOLDOUT is diagnostic, not approval.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

FEE = 0.01
SLIPPAGE = 0.001
NOTIONAL = 50.0
MAX_AGE_SEC = 900
MAX_GAP_SEC = 1200
MIN_TRAIN = 25
MIN_HOLDOUT = 12
EXIT_POLICIES = ("early_stop_1_5", "take_profit_4", "profit_lock_1_5",
                 "tight_trail_after_4")


def number(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (ValueError, TypeError, OverflowError):
        return None


def stamp(x):
    if not isinstance(x, str):
        return None
    try:
        d = datetime.fromisoformat(x.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, OverflowError):
        return None


def net_exit(entry, exit_price, notional=NOTIONAL):
    """Same paper economics as engine_legacy.close(), normalized to 50 SDA."""
    return notional * (exit_price / entry * (1 - SLIPPAGE) * (1 - FEE) - (1 + FEE))


def drawdown(values):
    balance = peak = worst = 0.0
    for x in values:
        balance += x
        peak = max(peak, balance)
        worst = max(worst, peak - balance)
    return round(worst, 4)


def summary(rows):
    values = [r["pnl_sda"] for r in rows]
    gains = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    return {
        "closed": len(rows),
        "net_sda": round(sum(values), 4),
        "win_rate_pct": round(100 * sum(x > 0 for x in values) / len(values), 2)
                        if values else None,
        "profit_factor": round(gains / losses, 4) if losses else None,
        "max_drawdown_sda": drawdown(values),
    }


def observed_trade(raw):
    if not isinstance(raw, dict) or str(raw.get("status") or "").upper() != "CLOSED":
        return None
    pnl = number(raw.get("closed_profit_sda"))
    close_time = stamp(raw.get("closed_at"))
    if pnl is None or close_time is None:
        return None
    entry = number(raw.get("entry_price"))
    close_price = number(raw.get("close_price"))
    investment = number(raw.get("investment_sda"))
    exit_reason = str(raw.get("close_reason") or "UNKNOWN")
    snap = raw.get("pump_entry_snapshot") if isinstance(raw.get("pump_entry_snapshot"), dict) else {}
    score = number(raw.get("entry_confidence"))
    mfe = number(raw.get("pump_mfe_pct"))
    quote_gap = number(raw.get("pump_exit_observation_gap_seconds"))
    return {
        "time": close_time.isoformat(),
        "pnl_sda": pnl,
        "entry_price": entry, "close_price": close_price,
        "investment_sda": investment,
        "exit_reason": exit_reason,
        "score": score, "mfe_pct": mfe,
        "exit_observation_gap_seconds": quote_gap,
        "entry": snap,
        "symbol": str(raw.get("label") or raw.get("symbol") or "UNKNOWN"),
        "source": raw,
    }


def _bucket_score(x):
    return "unknown" if x is None else ("below_70" if x < 70 else
                                         "70_to_84" if x < 85 else "85_plus")


def _bucket_mfe(x):
    return "unknown" if x is None else ("below_1_5pct" if x < 1.5 else
                                         "1_5_to_4pct" if x < 4 else "4pct_plus")


def _bucket_gap(x):
    return "unknown" if x is None else ("under_5m" if x <= 300 else
                                        "5_to_15m" if x <= 900 else "over_15m")


def _cohorts(rows, grouping):
    groups = defaultdict(list)
    for row in rows:
        groups[grouping(row)].append(row)
    return {k: summary(v) for k, v in sorted(groups.items())}


def quote_path(row):
    """Require future-only, fresh and gap-limited scanner observations.

    Legacy samples without explicit quote age are NOT replayable.
    """
    source = row["source"]
    entry = row["entry_price"]
    close = row["close_price"]
    inv = row["investment_sda"]
    opened, closed = stamp(source.get("opened_at")), stamp(source.get("closed_at"))
    if not all(x is not None for x in (entry, close, inv, opened, closed)):
        return None, "missing_entry_exit_or_timestamps"
    if min(entry, close, inv) <= 0 or closed <= opened:
        return None, "invalid_entry_exit"
    # Only current fully-closed V30 observations; partial and older exit
    # logic cannot be used as an interchangeable V30 baseline.
    if "V30" not in str(source.get("v30_mode") or ""):
        return None, "non_v30_trade"
    original = row["pnl_sda"]
    aligned = net_exit(entry, close, inv)
    if abs(original - aligned) > max(0.05, inv * 0.001):
        return None, "paper_fee_alignment_failed"
    samples = source.get("pump_price_samples")
    if not isinstance(samples, list) or len(samples) < 2:
        return None, "missing_price_samples"
    path = []
    for sample in samples:
        if not isinstance(sample, dict):
            return None, "malformed_price_sample"
        at = stamp(sample.get("at"))
        price = number(sample.get("price_sda"))
        age = number(sample.get("quote_age_seconds"))
        if at is None or price is None or price <= 0:
            return None, "invalid_sample_price_or_time"
        if age is None:
            return None, "missing_quote_age"
        if not 0 <= age <= MAX_AGE_SEC:
            return None, "stale_sample_quote"
        if at < opened or at > closed:
            return None, "sample_outside_actual_trade"
        if path and at <= path[-1][0]:
            return None, "sample_times_not_increasing"
        if path and (at - path[-1][0]).total_seconds() > MAX_GAP_SEC:
            return None, "sample_gap_over_20m"
        path.append((at, price))
    if (path[0][0] - opened).total_seconds() > MAX_AGE_SEC:
        return None, "missing_early_observation"
    if (closed - path[-1][0]).total_seconds() > MAX_GAP_SEC:
        return None, "missing_close_observation"
    return path, None


def earlier_exit(policy, entry, path, actual_close):
    """No sample after actual exit can be used; fall back to the real close."""
    peak = entry
    for at, price in path:
        peak = max(peak, price)
        gross_roi = (price / entry - 1) * 100
        peak_roi = (peak / entry - 1) * 100
        trigger = (
            policy == "early_stop_1_5" and gross_roi <= -1.5
            or policy == "take_profit_4" and gross_roi >= 4.0
            or policy == "profit_lock_1_5" and peak_roi >= 1.5 and gross_roi <= 0.3
            or policy == "tight_trail_after_4" and peak_roi >= 4.0
               and price <= peak * 0.985
        )
        if trigger:
            return price, at.isoformat(), True
    return actual_close, None, False


def _research_row(row, path):
    baseline = net_exit(row["entry_price"], row["close_price"])
    out = {"at": row["time"], "symbol": row["symbol"], "pnl_sda": baseline}
    for name in EXIT_POLICIES:
        exit_price, _, changed = earlier_exit(
            name, row["entry_price"], path, row["close_price"])
        out[name] = net_exit(row["entry_price"], exit_price)
        out[name + "_early"] = changed
    return out


def _experiment(rows, policy):
    baseline = summary([{"pnl_sda": x["pnl_sda"]} for x in rows])
    challenger = summary([{"pnl_sda": x[policy]} for x in rows])
    return {
        "same_trade_count": len(rows),
        "baseline": baseline, "candidate": challenger,
        "difference_sda": round(challenger["net_sda"] - baseline["net_sda"], 4),
        "earlier_exits": sum(x[policy + "_early"] for x in rows),
    }


def analyze(archive, stats=None, now=None):
    if not isinstance(archive, list):
        raise ValueError("Closed trade archive must be a list")
    if not isinstance(stats, (dict, type(None))):
        raise ValueError("Paper stats must be an object")
    now = now or datetime.now(timezone.utc)
    rows = [t for src in archive if (t := observed_trade(src)) is not None]
    rows.sort(key=lambda r: (r["time"], r["symbol"]))
    report = {
        "version": "V31-PROFIT-LEARNING-1",
        "generated_at": now.isoformat(),
        "mode": "READ_ONLY_PAPER_RESEARCH",
        "paper_only": True, "real_trading": False,
        "production_mutated": False, "promotion_eligible": False,
        "automatically_applied": False,
        "baseline_is_actual_observed_trades": True,
        "counterfactual_scope": "earlier_exits_on_same_observed_trades_only",
        "historical_observed": {
            "overall": summary(rows),
            "by_exit": _cohorts(rows, lambda r: r["exit_reason"]),
            "by_entry_score": _cohorts(rows, lambda r: _bucket_score(r["score"])),
            "by_mfe": _cohorts(rows, lambda r: _bucket_mfe(r["mfe_pct"])),
            "by_exit_observation_gap": _cohorts(rows, lambda r: _bucket_gap(r["exit_observation_gap_seconds"])),
            "gave_back_profit": {
                "mfe_at_least_2_5_and_closed_loss": sum(
                    r["mfe_pct"] is not None and r["mfe_pct"] >= 2.5
                    and r["pnl_sda"] <= 0 for r in rows),
                "measured_mfe_records": sum(r["mfe_pct"] is not None for r in rows),
            },
        },
        "data_quality": {"archive_closed_usable": len(rows), "price_paths_checked": 0,
                         "replayable_paths": 0, "excluded_by_reason": {}},
        "exit_research": {},
        "limitations": [
            "Historical realized P/L comes from trades placed by the old Champion.",
            "Earlier-only observed-price experiments cannot estimate missed BUYs or later exits.",
            "Sampled prices are not guaranteed executable fills or intrabar highs/lows.",
            "Never promote an exit policy from this research alone; independent forward validation required.",
        ],
    }
    if isinstance(stats, dict):
        expected = number(stats.get("realized_pnl_sda"))
        count = number(stats.get("closed_trades"))
        report["statistics_reconciled"] = bool(
            expected is not None and count is not None and int(count) == len(rows)
            and abs(expected - sum(r["pnl_sda"] for r in rows)) < 0.1)
        report["latest_stats_time"] = stats.get("updated_at")
        if not report["statistics_reconciled"]:
            report["limitations"].append(
                "Closed-trade archive and latest paper_stats do not match; aggregate may lag.")
    replayable = []
    exclusions = defaultdict(int)
    for row in rows:
        path, reason = quote_path(row)
        report["data_quality"]["price_paths_checked"] += 1
        if reason:
            exclusions[reason] += 1
        else:
            replayable.append(_research_row(row, path))
    report["data_quality"]["replayable_paths"] = len(replayable)
    report["data_quality"]["excluded_by_reason"] = dict(sorted(exclusions.items()))
    replayable.sort(key=lambda x: (x["at"], x["symbol"]))
    cutoff = int(len(replayable) * 0.7)
    train, holdout = replayable[:cutoff], replayable[cutoff:]
    report["exit_research"] = {
        "train_count": len(train), "holdout_count": len(holdout),
        "train_policy_tests": {p: _experiment(train, p) for p in EXIT_POLICIES},
        "selection_uses_train_only": True,
        "holdout_exploratory": True,
        "selected_candidate": None,
        "holdout": None,
        "status": "COLLECTING_VALIDATED_EXIT_PATHS",
    }
    if len(train) >= MIN_TRAIN and len(holdout) >= MIN_HOLDOUT:
        # Select one candidate on TRAIN, never on HOLDOUT. Even when
        # profitable this hypothesis remains research-only.
        ranked = sorted(
            EXIT_POLICIES, key=lambda p: (
                -report["exit_research"]["train_policy_tests"][p]["difference_sda"],
                p))
        best = ranked[0]
        report["exit_research"].update({
            "selected_candidate": best,
            "holdout": _experiment(holdout, best),
            "status": "OBSERVATIONAL_EARLIER_EXIT_ONLY",
        })
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="strategy_lab/closed_trades.json")
    ap.add_argument("--stats", default="paper_stats.json")
    ap.add_argument("--output", default="strategy_lab/v31_profit_learning.json")
    args = ap.parse_args()
    archive = json.loads(Path(args.archive).read_text(encoding="utf-8"))
    stats = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    result = analyze(archive, stats)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    temp.replace(output)
    print(json.dumps({
        "status": result["exit_research"]["status"],
        "closed": result["historical_observed"]["overall"]["closed"],
        "replayable_paths": result["data_quality"]["replayable_paths"],
        "excluded_reasons": result["data_quality"]["excluded_by_reason"],
        "promotion_eligible": False, "production_mutated": False,
    }))


if __name__ == "__main__":
    main()
