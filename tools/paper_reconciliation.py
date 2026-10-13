#!/usr/bin/env python3
"""Non-mutating SDA paper-ledger reconciliation.

The LIVE paper accounting source is positions.json / closed_trades. The
historical closed_trades.json file is an APPEND-ONLY UNION, not an accounting
ledger, and must never be added to current realized P/L. Preserve the union
while reporting exact identities present only on either side.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

IDENTITY_FIELDS = ("address", "opened_at", "closed_at")
SAMPLE_LIMIT = 12


def amount(value):
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def identity(row):
    if not isinstance(row, dict):
        return None
    parts = tuple(str(row.get(name) or "").strip() for name in IDENTITY_FIELDS)
    if not all(parts):
        return None
    return (parts[0].lower(), parts[1], parts[2])


def indexed(rows):
    result = {}
    duplicate_keys = Counter()
    unkeyed = 0
    for row in rows:
        key = identity(row)
        if key is None:
            unkeyed += 1
            continue
        if key in result:
            duplicate_keys[key] += 1
        result[key] = row
    return result, duplicate_keys, unkeyed


def reconcile(ledger, archive, stats, now=None):
    if not isinstance(ledger, dict) or not isinstance(ledger.get("closed_trades"), list):
        raise ValueError("Current paper positions ledger is missing or invalid")
    if not isinstance(archive, list):
        raise ValueError("Durable paper history must be a list")
    if not isinstance(stats, dict):
        raise ValueError("Paper statistics must be a dictionary")

    current = ledger["closed_trades"]
    current_index, current_duplicates, current_unkeyed = indexed(current)
    archive_index, archive_duplicates, archive_unkeyed = indexed(archive)
    historic_only = sorted(set(archive_index)-set(current_index))
    current_only = sorted(set(current_index)-set(archive_index))
    changed = []
    for key in set(current_index) & set(archive_index):
        now_pnl = amount(current_index[key].get("closed_profit_sda"))
        old_pnl = amount(archive_index[key].get("closed_profit_sda"))
        if now_pnl != old_pnl:
            changed.append(key)
    values = [amount(r.get("closed_profit_sda")) for r in current
              if isinstance(r, dict)]
    invalid_amounts = sum(x is None for x in values)
    raw_realized = sum(x for x in values if x is not None)
    stat_pnl = amount(stats.get("realized_pnl_sda"))
    stat_count = amount(stats.get("closed_trades"))
    stat_wins = amount(stats.get("wins"))
    stat_losses = amount(stats.get("losses"))
    ledger_wins = sum(x > 0 for x in values if x is not None)
    ledger_losses = sum(x < 0 for x in values if x is not None)
    # Identity and archive differences are independent from source accounting.
    # Current P/L is never overwritten by archived-only rows.
    count_match = stat_count is not None and stat_count == len(current)
    pnl_match = (stat_pnl is not None and not invalid_amounts
                 and abs(stat_pnl-raw_realized) < .001)
    wins_match = stat_wins is not None and stat_wins == ledger_wins
    losses_match = stat_losses is not None and stat_losses == ledger_losses
    current_accounting_match = bool(count_match and pnl_match and wins_match and losses_match)
    historic_profit = sum(amount(archive_index[k].get("closed_profit_sda")) or 0.
                        for k in historic_only)
    invalid_closed_status = sum(
        not isinstance(r, dict) or str(r.get("status") or "").upper() != "CLOSED"
        for r in current)
    diagnostic = {
        "version": "PAPER_LEDGER_RECONCILIATION_1",
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "mode": "READ_ONLY_PAPER_AUDIT", "paper_only": True,
        "production_mutated": False, "promotion_eligible": False,
        "current_pnl_source": "positions.json/closed_trades",
        "historic_archive_role": "append_only_union_for_audit_NOT_current_pnl",
        "stats_at": stats.get("updated_at"),
        "current_ledger": {
            "closed_rows": len(current),
            "keyed_unique": len(current_index),
            "duplicate_identity_rows": sum(current_duplicates.values()),
            "missing_identity_rows": current_unkeyed,
            "nonclosed_status_rows": invalid_closed_status,
            "invalid_pnl_rows": invalid_amounts,
            "calculated_realized_pnl_sda": round(raw_realized, 5),
            "calculated_wins": ledger_wins, "calculated_losses": ledger_losses,
        },
        "published_stats": {
            "closed_rows": stats.get("closed_trades"),
            "realized_pnl_sda": stats.get("realized_pnl_sda"),
            "wins": stats.get("wins"), "losses": stats.get("losses"),
        },
        "current_accounting_reconciled": current_accounting_match,
        "archive": {
            "total_rows": len(archive),
            "keyed_unique": len(archive_index),
            "duplicate_identity_rows": sum(archive_duplicates.values()),
            "missing_identity_rows": archive_unkeyed,
            "historic_only_count": len(historic_only),
            "current_only_count": len(current_only),
            "same_identity_different_pnl_count": len(changed),
            "historic_only_sum_sda": round(historic_profit, 5),
            "historic_only_samples": [
                {"address": k[0], "opened_at": k[1], "closed_at": k[2],
                 "pnl_sda": amount(archive_index[k].get("closed_profit_sda"))}
                for k in historic_only[:SAMPLE_LIMIT]
            ],
            "current_only_samples": [
                {"address": k[0], "opened_at": k[1], "closed_at": k[2]}
                for k in current_only[:SAMPLE_LIMIT]
            ],
        },
        "historical_archive_matches_current_ledger": not (
            historic_only or current_only or changed or current_duplicates
            or archive_duplicates or current_unkeyed or archive_unkeyed),
        "warnings": [],
    }
    if not current_accounting_match:
        diagnostic["warnings"].append(
            "Published paper_stats does not match current positions closed rows; "
            "do not treat research as reconciled.")
    if historic_only:
        diagnostic["warnings"].append(
            "Append-only research archive has trades absent from CURRENT paper "
            "ledger. Keep them for audit; do NOT add them to current P/L.")
    if current_only:
        diagnostic["warnings"].append(
            "Some current closed trades are absent from historical archive.")
    if changed:
        diagnostic["warnings"].append(
            "Same trade identity has conflicting saved P/L values across ledgers.")
    if invalid_closed_status or invalid_amounts or current_duplicates or current_unkeyed:
        diagnostic["warnings"].append(
            "Current paper closed records contain missing/duplicate identities, "
            "invalid status or amount; investigation required.")
    return diagnostic


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="positions.json")
    ap.add_argument("--archive", default="strategy_lab/closed_trades.json")
    ap.add_argument("--stats", default="paper_stats.json")
    ap.add_argument("--output", default="strategy_lab/paper_reconciliation.json")
    args = ap.parse_args()
    source = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    historical = json.loads(Path(args.archive).read_text(encoding="utf-8"))
    stats = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    result = reconcile(source, historical, stats)
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix+".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    tmp.replace(dest)
    print("PAPER ACCOUNTING RECONCILIATION:", json.dumps({
        "current_closed": result["current_ledger"]["closed_rows"],
        "archived": result["archive"]["total_rows"],
        "historical_only": result["archive"]["historic_only_count"],
        "historical_only_sda": result["archive"]["historic_only_sum_sda"],
        "current_accounting_reconciled": result["current_accounting_reconciled"],
        "warnings": result["warnings"],
    }, ensure_ascii=False))
    if not result["current_accounting_reconciled"]:
        print("::warning::Current ledger and paper_stats mismatch; inspect reconciliation JSON")
    if not result["historical_archive_matches_current_ledger"]:
        print("::warning::Historical union differs from CURRENT paper ledger; archived-only records preserved")


if __name__ == "__main__":
    main()
