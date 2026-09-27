#!/usr/bin/env python3
"""Read-only V30.3 paper trade telemetry summary; never places orders."""
import json
from collections import Counter
from pathlib import Path

def report(path):
    p=Path(path)
    if not p.is_file() or not p.stat().st_size:
        return {"status":"no_persisted_positions_file","note":"No history available in this checkout."}
    data=json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data,dict):
        raise ValueError("positions root must be an object")
    closed=data.get("closed_trades",[]) or []
    open_positions=data.get("positions",{}) or {}
    if not isinstance(closed,list) or not isinstance(open_positions,dict):
        raise ValueError("invalid positions schema")
    annotated=[x for x in closed if isinstance(x,dict) and x.get("pump_exit_snapshot")]
    exit_reasons=Counter(str(x.get("close_reason","UNKNOWN")) for x in annotated)
    def number(v):
        try:return float(v)
        except (ValueError,TypeError):return 0.0
    return {
        "status":"ok","closed_count":len(closed),"open_count":len(open_positions),
        "annotated_closed_count":len(annotated),
        "annotated_realized_pnl_sda":round(sum(number(x.get("closed_profit_sda")) for x in annotated),4),
        "annotated_exit_reasons":dict(exit_reasons),
        "last_20_annotated":[{
            "label":x.get("label"),"closed_at":x.get("closed_at"),
            "reason":x.get("close_reason"),"net_pnl_sda":x.get("closed_profit_sda"),
            "net_roi_pct":x.get("closed_roi_pct"),
            "observed_mfe_pct":x.get("pump_mfe_pct"),
            "observed_mae_pct":x.get("pump_mae_pct"),
            "observed_samples":len(x.get("pump_price_samples") or []),
            "exit_scan_gap_seconds":x.get("pump_exit_observation_gap_seconds"),
            "exit_stop_gap_pct":x.get("pump_exit_stop_gap_pct"),
        } for x in annotated[-20:]]
    }

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("positions",nargs="?",default="positions.json")
    args=parser.parse_args()
    print(json.dumps(report(args.positions),ensure_ascii=False,indent=2))
