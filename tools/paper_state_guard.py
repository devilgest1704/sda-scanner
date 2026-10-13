#!/usr/bin/env python3
"""Fail-closed paper accounting guard for the long-running scanner worker.

Never reset a missing or malformed CURRENT positions.json or paper_stats.json
to {}. Require current accounting to match exactly at each scan checkpoint.
Within a worker, a previously recorded closed-trade identity must NEVER
disappear; if it does, abort rather than checkpoint a partially lost ledger.
The small sentinel exists only on the worker VM, not in the repository.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.paper_reconciliation import identity, reconcile

DEFAULT_SENTINEL = Path("/tmp/sda-scanner-paper-closed-identity-guard.json")


def validate(ledger, stats, sentinel):
    if not isinstance(ledger, dict) or not isinstance(ledger.get("positions"), dict):
        raise ValueError("CURRENT positions.json missing or invalid open positions")
    if not isinstance(ledger.get("closed_trades"), list):
        raise ValueError("CURRENT positions.json missing closed_trades list")
    if not isinstance(stats, dict):
        raise ValueError("CURRENT paper_stats.json must be an object")
    checked = reconcile(ledger, [], stats)
    current = checked["current_ledger"]
    if not checked["current_accounting_reconciled"]:
        raise ValueError("CURRENT paper_stats and positions ledger disagree; refusing checkpoint")
    if (current["duplicate_identity_rows"] or current["missing_identity_rows"] or
            current["nonclosed_status_rows"] or current["invalid_pnl_rows"]):
        raise ValueError("CURRENT paper ledger has duplicate/incomplete closed trades")
    now = {identity(r) for r in ledger["closed_trades"]}
    if None in now:
        raise ValueError("Closed trade identity missing")
    # Keys use their original timestamp strings; do not normalize away distinct
    # legitimate partial fills, which have different close timestamps.
    serialized = {"version": 1, "closed_keys": [list(k) for k in sorted(now)]}
    sentinel = Path(sentinel)
    if sentinel.exists():
        previous = json.loads(sentinel.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("version") != 1:
            raise ValueError("Paper identity sentinel corrupt; refusing checkpoint")
        old_keys = previous.get("closed_keys")
        if not isinstance(old_keys, list):
            raise ValueError("Paper identity sentinel has no closed identity set")
        old = set()
        for key in old_keys:
            if not isinstance(key, list) or len(key) != 3 or any(not isinstance(k, str) for k in key):
                raise ValueError("Paper identity sentinel has invalid identity")
            old.add(tuple(key))
        lost = old-now
        if lost:
            raise ValueError(
                f"CURRENT paper ledger lost {len(lost)} previously observed "
                f"closed trades. Example address {sorted(lost)[0][0]}; "
                "refusing to overwrite last valid checkpoint.")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    # Never reset the sentinel: atomic write after all validations only.
    tmp = sentinel.with_suffix(sentinel.suffix + ".tmp")
    tmp.write_text(json.dumps(serialized, ensure_ascii=False,
                              separators=(",", ":"))+"\n", encoding="utf-8")
    tmp.replace(sentinel)
    return {
        "current_closed": len(ledger["closed_trades"]),
        "realized_pnl_sda": current["calculated_realized_pnl_sda"],
        "identities_preserved": True, "paper_stats_match": True,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", default="positions.json")
    p.add_argument("--stats", default="paper_stats.json")
    p.add_argument("--sentinel", default=str(DEFAULT_SENTINEL))
    a = p.parse_args()
    # No fallback to {}. This guard must FAIL if these crucial states cannot
    # be read or parsed. The watchdog can then recover from the prior commit.
    ledger = json.loads(Path(a.ledger).read_text(encoding="utf-8"))
    stats = json.loads(Path(a.stats).read_text(encoding="utf-8"))
    print("PAPER LEDGER GUARD:", json.dumps(validate(ledger, stats, a.sentinel)))


if __name__ == "__main__":
    main()
