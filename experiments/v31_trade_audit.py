"""Read-only audit of V30 closed paper trades and entry snapshots.

Usage: python -m experiments.v31_trade_audit positions.json
Never changes trading state. Missing data is reported, not invented.
"""
import json
import sys
from collections import Counter
from pathlib import Path

def analyze(data):
    if not isinstance(data,dict):
        raise ValueError("positions.json must be an object")
    trades=data.get("closed_trades")
    if not isinstance(trades,list):
        raise ValueError("Missing closed_trades array")
    reasons=Counter()
    complete=[]
    missing=Counter()
    for t in trades:
        if not isinstance(t,dict):
            missing["invalid_trade"]+=1
            continue
        reasons[str(t.get("close_reason") or "UNKNOWN")]+=1
        snap=t.get("pump_entry_snapshot")
        if not isinstance(snap,dict):
            missing["pump_entry_snapshot"]+=1
        else:
            for key in ("quality","m15","m1h","flow_1h","volume_1h","trades_1h"):
                if snap.get(key) is None:
                    missing["snapshot."+key]+=1
        try:
            pnl=float(t["closed_profit_sda"])
            if not __import__("math").isfinite(pnl): raise ValueError()
        except (KeyError,TypeError,ValueError):
            missing["closed_profit_sda"]+=1
            continue
        complete.append((t,pnl))
    wins=[p for _,p in complete if p>0]
    losses=[p for _,p in complete if p<0]
    gross_win=sum(wins); gross_loss=-sum(losses)
    return {
        "closed_records":len(trades),
        "pnl_records":len(complete),
        "realized_pnl_sda":round(sum(p for _,p in complete),6),
        "wins":len(wins),"losses":len(losses),
        "breakeven":len(complete)-len(wins)-len(losses),
        "win_rate_pct":round(100*len(wins)/len(complete),2) if complete else None,
        "avg_win_sda":round(gross_win/len(wins),4) if wins else None,
        "avg_loss_sda":round(-gross_loss/len(losses),4) if losses else None,
        "profit_factor":round(gross_win/gross_loss,4) if gross_loss else None,
        "exit_reasons":dict(reasons),
        "missing_fields":dict(missing),
        "warning":"Partial closes may be counted as separate records; verify reconciliation with paper_stats.json before optimizing."
    }

def main():
    path=Path(sys.argv[1] if len(sys.argv)>1 else "positions.json")
    result=analyze(json.loads(path.read_text(encoding="utf-8")))
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=="__main__":
    main()
