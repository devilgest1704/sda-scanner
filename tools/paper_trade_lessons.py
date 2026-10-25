#!/usr/bin/env python3
"""What the executed SDA paper trades actually teach us, without hindsight.

All aggregates are descriptive of incumbent trades only. This tool never
changes the live/paper entry or exit settings; it does not claim a strategy
variant would have earned these observed amounts.
"""
from __future__ import annotations
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def num(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None


def normalize_trade(row):
    if not isinstance(row,dict) or str(row.get("status","")).upper() != "CLOSED":
        return None
    pnl=num(row.get("closed_profit_sda"))
    closed=str(row.get("closed_at") or "")
    if pnl is None or not closed:
        return None
    label=str(row.get("label") or row.get("symbol") or row.get("address") or "UNKNOWN")
    reason=str(row.get("close_reason") or row.get("exit_reason") or row.get("reason") or "UNKNOWN")
    roi=num(row.get("closed_roi_pct"))
    score=num(row.get("entry_confidence"))
    return {"label":label,"closed_at":closed,"pnl_sda":round(pnl,5),
            "roi_pct":round(roi,4) if roi is not None else None,
            "exit_reason":reason,"entry_score":score,
            "address":str(row.get("address") or "").lower()}


def summary(trades):
    n=len(trades)
    winners=[t["pnl_sda"] for t in trades if t["pnl_sda"]>0]
    losers=[t["pnl_sda"] for t in trades if t["pnl_sda"]<0]
    total=sum(t["pnl_sda"] for t in trades)
    avg_win=sum(winners)/len(winners) if winners else 0.0
    avg_loss=-sum(losers)/len(losers) if losers else 0.0
    denom=avg_win+avg_loss
    return {"closed":n,"wins":len(winners),"losses":len(losers),
            "breakeven":n-len(winners)-len(losers),
            "net_pnl_sda":round(total,4),
            "mean_pnl_per_trade_sda":round(total/n,4) if n else None,
            "win_rate_pct":round(100*len(winners)/n,2) if n else None,
            "avg_win_sda":round(avg_win,4) if winners else None,
            "avg_loss_abs_sda":round(avg_loss,4) if losers else None,
            "breakeven_win_rate_pct":round(100*avg_loss/denom,2) if denom else None,
            "profit_factor":round(sum(winners)/-sum(losers),4) if losers else None}


def analyze(source_rows, stats=None):
    if not isinstance(source_rows,list):
        raise ValueError("Trade archive must be a list")
    rows=[t for raw in source_rows if (t:=normalize_trade(raw)) is not None]
    rows.sort(key=lambda r:(r["closed_at"],r["address"],r["label"]))
    by_exit=defaultdict(list)
    for row in rows:
        by_exit[row["exit_reason"]].append(row)
    latest=rows[-20:]
    last50=rows[-50:]
    score_bands={"below_70":[],"70_to_79":[],"80_to_89":[],"90_plus":[],"unknown":[]}
    for trade in last50:
        score=trade["entry_score"]
        band=("unknown" if score is None else "below_70" if score<70
              else "70_to_79" if score<80 else "80_to_89" if score<90 else "90_plus")
        score_bands[band].append(trade)
    recent_exits=defaultdict(list)
    for trade in last50:
        recent_exits[trade["exit_reason"]].append(trade)
    pine=[r for r in rows if r["label"].upper().split("/")[0].strip()=="PINE"]
    # Include the last trade even if label came from an address-only record?
    out={
        "version":"PAPER-TRADE-LESSONS-1",
        "generated_at":datetime.now(timezone.utc).isoformat(),
        "mode":"READ_ONLY_PAPER_OBSERVED",
        "strategy_changed":False,
        "validates_counterfactual":False,
        "limitations":["Only trades actually placed by the incumbent are included",
                       "Exit category correlations do not show causal superiority",
                       "Historical P/L is not an alternative strategy replay"],
        "overall":summary(rows),
        "by_exit_reason":[{"reason":reason,**summary(group)}
                          for reason,group in sorted(by_exit.items(),
                             key=lambda pair:(sum(t["pnl_sda"] for t in pair[1]),pair[0]))],
        "recent":latest[::-1],
        "recent_50":{"summary":summary(last50),
                     "by_entry_score":[{"band":band,**summary(group)} for band,group in score_bands.items()],
                     "by_exit_reason":[{"reason":reason,**summary(group)} for reason,group in sorted(recent_exits.items())],
                     "limitations":"Observed trades only; no counterfactual BUY backtest"},
        "pine_recent":pine[-1] if pine else None,
    }
    if isinstance(stats,dict):
        expected=num(stats.get("realized_pnl_sda"))
        count=num(stats.get("closed_trades"))
        out["statistics_reconciled"]=(
            expected is not None and count is not None
            and int(count)==len(rows) and abs(expected-out["overall"]["net_pnl_sda"])<0.1)
        out["statistics_closed_trades"]=int(count) if count is not None else None
        out["statistics_net_sda"]=round(expected,4) if expected is not None else None
    return out


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--ledger",default="positions.json",
                        help="Current paper ledger only; historical union is not official P/L")
    parser.add_argument("--stats",default="paper_stats.json")
    parser.add_argument("--output",default="strategy_lab/trade_lessons.json")
    args=parser.parse_args()
    ledger=json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    if not isinstance(ledger,dict) or not isinstance(ledger.get("closed_trades"),list):
        raise ValueError("Current paper ledger is invalid or missing closed trades")
    stats=json.loads(Path(args.stats).read_text(encoding="utf-8"))
    report=analyze(ledger["closed_trades"],stats)
    report["accounting_source"]="CURRENT_positions_json_closed_trades_only"
    report["historical_union_included"]=False
    dst=Path(args.output)
    dst.parent.mkdir(parents=True,exist_ok=True)
    dst.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({
       "closed":report["overall"]["closed"],
       "net_sda":report["overall"]["net_pnl_sda"],
       "stats_match":report.get("statistics_reconciled"),
       "pine":report["pine_recent"],
       "worst_exit_categories":report["by_exit_reason"][:5],
       "strategy_changed":False},ensure_ascii=False))
    # Ledger mismatch should be a warning, not a silent claim of clean data.
    if report.get("statistics_reconciled") is False:
        print("::warning::CURRENT positions closed trades do not match paper_stats; investigate inconsistent checkpoint")


if __name__=="__main__":
    main()
