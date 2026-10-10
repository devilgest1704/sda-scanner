#!/usr/bin/env python3
"""Research-only chronological trade cohort analysis; NOT a counterfactual backtest.

Only trades executed by the incumbent strategy are observable. Tightening entry
score can filter that cohort, but cannot predict fills, missed trades, or the
performance of loosened gates. No production promotion is authorized.
"""
import argparse
import json
import math
from pathlib import Path

def number(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None

def evaluate(rows, threshold=75, min_train=30, min_test=20):
    eligible=[]
    for row in rows:
        if not isinstance(row,dict) or row.get("status")!="CLOSED": continue
        score=number(row.get("entry_confidence"))
        pnl=number(row.get("closed_profit_sda"))
        stamp=row.get("closed_at")
        if score is None or pnl is None or not isinstance(stamp,str) or not stamp:continue
        eligible.append((stamp,score,pnl))
    eligible.sort(key=lambda x:x[0])
    cutoff=int(len(eligible)*0.7)
    train,test=eligible[:cutoff],eligible[cutoff:]
    def summarize(data):
        baseline=sum(p for _,_,p in data)
        retained=[p for _,s,p in data if s>=threshold]
        return {"observed_trades":len(data),"observed_baseline_pnl_sda":round(baseline,4),
                "retained_trades":len(retained),"retained_pnl_sda":round(sum(retained),4),
                "excluded_observed_pnl_sda":round(baseline-sum(retained),4),
                "retained_win_rate_pct":round(100*sum(p>0 for p in retained)/len(retained),2) if retained else None}
    enough=len(train)>=min_train and len(test)>=min_test
    return {"method":"observed_cohort_filter_not_backtest","parameter":"entry_confidence",
            "threshold":threshold,"sample_count":len(eligible),"train":summarize(train),
            "holdout":summarize(test),"sufficient_sample":enough,
            "promotion_eligible":False,"production_mutated":False,
            "limitations":["Selection bias: only executed incumbent trades exist",
                           "No missed-trade or altered-portfolio replay",
                           "No claim of out-of-sample strategy profitability"]}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--archive",default="strategy_lab/closed_trades.json")
    p.add_argument("--output",default="strategy_lab/research.json")
    p.add_argument("--threshold",type=float,default=75)
    args=p.parse_args()
    if not 68<=args.threshold<=85:raise ValueError("threshold outside paper bounds")
    src=Path(args.archive)
    rows=json.loads(src.read_text(encoding="utf-8")) if src.exists() else []
    if not isinstance(rows,list):raise ValueError("archive must be list")
    result=evaluate(rows,args.threshold)
    dst=Path(args.output);dst.parent.mkdir(parents=True,exist_ok=True)
    dst.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({"samples":result["sample_count"],"sufficient":result["sufficient_sample"],
                      "promotion_eligible":False}))
if __name__=="__main__":main()
