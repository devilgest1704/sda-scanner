"""Offline SHADOW signal diagnostics for recovery-v2; no trading or writes.

Usage:
  python -m strategy_lab.recovery_v2_shadow_diagnostic --market market_data.json --funnel v30_no_buy_diagnostic.json
The output is JSON on stdout. This does not start the live forward SHADOW agent.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from strategy_lab.recovery_v2_snapshot_adapter import inspect_snapshot


def diagnose(market, funnel):
    at = funnel.get("at") or funnel.get("checked_at")
    tokens = market.get("tokens", {})
    candidates = funnel.get("candidate_snapshots", [])
    if not isinstance(tokens, dict) or not isinstance(candidates, list):
        return {"status": "invalid_inputs", "ready": 0, "reasons": {}}
    reasons = Counter()
    ready = 0
    for c in candidates:
        if not isinstance(c, dict):
            reasons["invalid_candidate"] += 1
            continue
        address = str(c.get("address") or "").lower()
        token = tokens.get(address)
        if not isinstance(token, dict):
            reasons["missing_market_token"] += 1
            continue
        decision = inspect_snapshot(c, token, at)
        if decision["ready"]:
            ready += 1
        else:
            reasons.update(decision["reasons"])
    return {"status": "research_only", "evaluated": len(candidates),
            "ready": ready, "reasons": dict(reasons),
            "paper_unchanged": True, "real_trading": False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--market", required=True)
    p.add_argument("--funnel", required=True)
    args = p.parse_args()
    market = json.loads(Path(args.market).read_text(encoding="utf-8"))
    funnel = json.loads(Path(args.funnel).read_text(encoding="utf-8"))
    print(json.dumps(diagnose(market, funnel), sort_keys=True))


if __name__ == "__main__":
    main()
