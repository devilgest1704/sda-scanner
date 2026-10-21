"""Export timestamp-aligned, frozen Recovery V2 research observations only.

No PAPER writes, trades, or fabricated missing features. Use same-scan inputs.
"""
import argparse
import json
from pathlib import Path
from strategy_lab.recovery_v2_shadow_diagnostic import diagnose
from strategy_lab.recovery_v2_snapshot_adapter import inspect_snapshot, _time


def export(market, funnel):
    source = funnel.get("funnel") if isinstance(funnel.get("funnel"), dict) else funnel
    at = source.get("at") or source.get("checked_at")
    market_at = market.get("updated_at")
    candidates = source.get("candidate_snapshots")
    tokens = market.get("tokens")
    if not isinstance(candidates, list) or not isinstance(tokens, dict):
        return {"status": "missing_frozen_candidates_or_tokens", "records": [], "paper_unchanged": True}
    a, b = _time(at), _time(market_at)
    if a is None or b is None or abs((a-b).total_seconds()) > 600 or b > a:
        return {"status": "unaligned_or_future_market", "records": [], "paper_unchanged": True}
    records = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        address = str(candidate.get("address") or "").lower()
        token = tokens.get(address)
        if not isinstance(token, dict):
            records.append({"address": address, "ready": False, "reasons": ["missing_market_token"]})
            continue
        decision = inspect_snapshot(candidate, token, at)
        records.append({"address": address, "decision_at": at, "market_at": market_at,
                        "ready": decision["ready"], "reasons": decision["reasons"]})
    return {"status": "research_only", "records": records, "evaluated": len(records),
            "ready": sum(bool(x["ready"]) for x in records),
            "paper_unchanged": True, "real_trading": False,
            "execution_parity_verified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", required=True)
    parser.add_argument("--funnel", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = export(json.loads(Path(args.market).read_text()),
                    json.loads(Path(args.funnel).read_text()))
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if result["status"] != "research_only":
        raise SystemExit("No valid frozen input: " + result["status"])


if __name__ == "__main__":
    main()
