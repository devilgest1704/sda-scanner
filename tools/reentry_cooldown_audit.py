#!/usr/bin/env python3
"""Read-only historical audit of repeat entries after a losing close.

This is NOT a portfolio backtest. The original ledger is never modified.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path


def stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def audit(trades, windows=(6, 12, 24, 48)):
    rows = []
    for row in trades:
        if not isinstance(row, dict) or row.get("status") != "CLOSED":
            continue
        opened, closed = stamp(row.get("opened_at")), stamp(row.get("closed_at"))
        try:
            pnl = float(row["closed_profit_sda"])
        except (KeyError, ValueError, TypeError):
            continue
        if opened is None or closed is None or not opened <= closed or not (-1e15 < pnl < 1e15):
            continue
        address = str(row.get("address") or "").lower()
        if address:
            rows.append((opened, closed, address, pnl))
    rows.sort()
    results = []
    for hours in windows:
        latest_close = {}
        excluded = []
        for opened, closed, address, pnl in rows:
            prior = latest_close.get(address)
            if prior and prior[1] < 0 and 0 <= opened - prior[0] < hours * 3600:
                excluded.append(pnl)
            if prior is None or closed > prior[0]:
                latest_close[address] = (closed, pnl)
        results.append({"cooldown_hours": hours, "historical_entries_flagged": len(excluded),
                        "flagged_observed_pnl_sda": round(sum(excluded), 4),
                        "flagged_winners": sum(p > 0 for p in excluded)})
    return {"mode": "READ_ONLY_HISTORICAL_DIAGNOSTIC",
            "not_counterfactual_portfolio_pnl": True,
            "eligible_closed_trades": len(rows), "windows": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default="positions.json")
    parser.add_argument("--output", default="strategy_lab/reentry_cooldown_audit.json")
    parser.add_argument("--forward-start", default="2026-10-26T20:15:00+00:00",
                        help="UTC ISO date: count only entries first opened after activation")
    args = parser.parse_args()
    ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    result = audit(ledger["closed_trades"])
    start = stamp(args.forward_start)
    if start is None:
        raise ValueError("Invalid --forward-start timestamp")
    forward_rows = [row for row in ledger["closed_trades"]
                    if isinstance(row, dict) and (stamp(row.get("opened_at")) or 0) >= start]
    # The forward cohort is descriptive: do not pretend excluded trades were
    # never executed or infer a replacement strategy P/L.
    forward = audit(forward_rows)
    result["forward_observed_since"] = args.forward_start
    result["forward_observed"] = forward
    result["forward_limitations"] = (
        "Forward observed executed PAPER entries only; not a parallel simulated "
        "portfolio. Prior closes before activation are not used for eligibility.")
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
