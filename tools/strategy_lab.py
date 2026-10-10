#!/usr/bin/env python3
"""AI Strategy Lab stage 1: read-only evidence gate, no live changes."""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

FILTERS = ("entry_score", "quality", "impulse", "buy_ratio", "trades")

def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)

def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def analyze(stats, execution, config):
    if execution.get("paper_only") is not True:
        raise ValueError("Refusing to analyze non-paper execution state")
    filters = config.get("filters", {})
    if any(not finite(filters.get(key)) for key in FILTERS):
        raise ValueError("Invalid baseline filters")
    events = execution.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Execution events must be a list")
    ready = [e for e in events if isinstance(e, dict) and e.get("event") == "READY"]
    created = [e for e in events if isinstance(e, dict) and e.get("event") == "CREATED"]
    pnl = stats.get("cumulative_pnl_sda")
    if not finite(pnl):
        raise ValueError("Missing finite cumulative SDA P/L")
    # READY/CREATED are NOT closed trade outcomes. Do not use these to claim
    # backtest performance, win rate for candidates or causal improvements.
    candidate = dict(filters)
    candidate["entry_score"] = min(85.0, round(filters["entry_score"] + 2, 2))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "paper_shadow_proposal_only",
        "production_mutated": False,
        "promotion_eligible": False,
        "reason": "No joined, timestamped entry/exit trade dataset or out-of-sample backtest",
        "baseline": {"filters": filters, "cumulative_pnl_sda": pnl,
                     "closed_trades": stats.get("closed_trades"),
                     "win_rate_pct": stats.get("win_rate_pct")},
        "observations": {"ready_events": len(ready), "created_events": len(created),
                         "events_retained": len(events)},
        "challenger": {"filters": candidate, "status": "UNVALIDATED",
                       "hypothesis": "A slightly higher entry score might reject weak entries; not proven"},
        "promotion_requirements": [
            "Persist immutable entry and exit fills with token, timestamps, fee and slippage",
            "Replay identical market snapshots for champion and challenger without lookahead",
            "Use chronological train/validation/test split and walk-forward testing",
            "Require sufficient independent trades, positive net SDA after costs and bounded drawdown",
            "Shadow-run before promotion; rollback on degraded outcomes"
        ]
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", default="paper_stats.json")
    parser.add_argument("--execution", default="v30_execution_state.json")
    parser.add_argument("--config", default="v30_agent_config.json")
    parser.add_argument("--output", default="strategy_lab/report.json")
    args = parser.parse_args()
    result = analyze(load(args.stats), load(args.execution), load(args.config))
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result["promotion_eligible"]:
        raise AssertionError("Stage 1 must never promote a strategy")

if __name__ == "__main__":
    main()
