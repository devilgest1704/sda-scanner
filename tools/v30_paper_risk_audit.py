#!/usr/bin/env python3
"""Read-only paper trading risk audit. Never places orders or changes strategy."""
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}")

def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

def summarize(rows):
    return {
        "trades": len(rows),
        "wins": sum(num(t.get("closed_profit_sda")) > 0 for t in rows),
        "pnl_sda": round(sum(num(t.get("closed_profit_sda")) for t in rows), 4),
        "mean_roi_pct": round(mean(num(t.get("closed_roi_pct")) for t in rows), 3) if rows else None,
    }

def audit(trades, limit=30):
    recent = trades[-limit:]
    by_phase, by_exit = defaultdict(list), defaultdict(list)
    exceptions = []
    for t in recent:
        by_phase[str(t.get("entry_phase") or "UNKNOWN")].append(t)
        by_exit[str(t.get("close_reason") or "UNKNOWN")].append(t)
        gap = t.get("pump_exit_observation_gap_seconds")
        stop_gap = t.get("pump_exit_stop_gap_pct")
        if (gap is not None and num(gap) > 120) or (stop_gap is not None and num(stop_gap) < -2):
            exceptions.append({
                "label": t.get("label"),
                "closed_at": t.get("closed_at"),
                "pnl_sda": round(num(t.get("closed_profit_sda")), 4),
                "roi_pct": round(num(t.get("closed_roi_pct")), 3),
                "exit_reason": t.get("close_reason"),
                "observation_gap_seconds": gap,
                "stop_gap_pct": stop_gap,
                "entry_phase": t.get("entry_phase"),
                "entry_score": (t.get("pump_entry_snapshot") or {}).get("score"),
                "entry_data_age_sec": (t.get("pump_entry_snapshot") or {}).get("data_age_sec"),
            })
    return {
        "paper_only": True,
        "source": "positions.json closed_trades",
        "window": len(recent),
        "summary": summarize(recent),
        "by_entry_phase": {k: summarize(v) for k, v in sorted(by_phase.items())},
        "by_exit_reason": {k: summarize(v) for k, v in sorted(by_exit.items())},
        "risk_exceptions": exceptions,
        "note": "Observation gaps measure time between recorded position evaluations, not exchange tick intervals. Stops are simulated at observed prices; no guaranteed execution price.",
    }

def main():
    data = load("positions.json")
    if not isinstance(data, dict) or not isinstance(data.get("closed_trades"), list):
        raise SystemExit("Invalid positions.json schema")
    report = audit(data["closed_trades"])
    print(json.dumps(report, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
