"""Read-only PAPER exit quote-age audit. Research only; never changes trading state.

Input: positions.json snapshot. An old quote flag alone does not prove a stale fill.
Usage: python -m strategy_lab.paper_exit_quote_audit --positions positions.json
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def audit(data, threshold_sec=300):
    if not isinstance(data, dict) or not isinstance(data.get("closed_trades"), list):
        return {"status": "invalid_input", "records": [], "review_count": 0}
    records = []
    for trade in data["closed_trades"]:
        if not isinstance(trade, dict):
            continue
        label = trade.get("label") or trade.get("symbol") or trade.get("address")
        closed_at = trade.get("closed_at")
        quote_at = trade.get("exit_quote_at") or trade.get("quote_at")
        end, quote = _parse(closed_at), _parse(quote_at)
        derived_age = (end - quote).total_seconds() if end and quote else None
        raw_age = trade.get("exit_quote_age_sec")
        if raw_age is None:
            raw_age = trade.get("quote_age_seconds")
        age = derived_age if derived_age is not None else (
            float(raw_age) if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool) else None
        )
        stale_flag = trade.get("pump_exit_data_stale") is True
        if stale_flag or (age is not None and (age > threshold_sec or age < 0)):
            records.append({
                "label": label, "closed_at": closed_at, "reason": trade.get("reason"),
                "stale_flag": stale_flag, "exit_quote_age_sec": age,
                "age_source": "timestamps" if derived_age is not None else (
                    "exit_metadata" if age is not None else "unknown"),
                "requires_quote_verification": True,
                "proven_bad_execution": False,
            })
    return {
        "status": "research_only", "closed_trades": len(data["closed_trades"]),
        "review_count": len(records), "records": records,
        "paper_unchanged": True, "execution_parity_verified": False,
        "caveat": "Historical stale flags can persist after fresh exits; verify quote timestamps and execution path.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--positions", required=True)
    parser.add_argument("--threshold-sec", type=int, default=300)
    args = parser.parse_args()
    if args.threshold_sec < 0:
        parser.error("--threshold-sec must be nonnegative")
    data = json.loads(Path(args.positions).read_text(encoding="utf-8"))
    print(json.dumps(audit(data, args.threshold_sec), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
