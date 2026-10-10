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


def explore_thresholds(rows, thresholds=(68,70,72,74,76,78,80,82,84)):
    """Choose a *research hypothesis* using TRAIN results only.

    The subsequent holdout is reported, not searched or used to choose the
    threshold. Historical retained-trade P/L is NOT a replay of a new policy.
    """
    results=[evaluate(rows,t) for t in thresholds]
    reference=results[0]
    train_count=reference["train"]["observed_trades"]
    min_retained=max(20, math.ceil(train_count*0.25))
    alternatives=[r for r in results
                  if r["sufficient_sample"]
                  and r["train"]["retained_trades"]>=min_retained]
    best=max(alternatives,
             key=lambda r:(r["train"]["retained_pnl_sda"],r["train"]["retained_trades"],-r["threshold"])) if alternatives else None
    return {
      "method":"train_only_threshold_search_observed_cohort_not_backtest",
      "sample_count":reference["sample_count"],
      "train_sample_count":train_count,
      "holdout_sample_count":reference["holdout"]["observed_trades"],
      "minimum_retained_train":min_retained,
      "tested_thresholds":list(thresholds),
      "recommended_entry_score":best["threshold"] if best else None,
      "selection_basis":"train_retained_observed_pnl_sda_only",
      "train":best["train"] if best else reference["train"],
      "holdout":best["holdout"] if best else reference["holdout"],
      "train_observed_difference_sda":round(best["train"]["retained_pnl_sda"]-best["train"]["observed_baseline_pnl_sda"],4) if best else None,
      "holdout_observed_difference_sda":round(best["holdout"]["retained_pnl_sda"]-best["holdout"]["observed_baseline_pnl_sda"],4) if best else None,
      "holdout_observed_retained_trades":best["holdout"]["retained_trades"] if best else None,
      "sufficient_sample":reference["sufficient_sample"],
      "production_mutated":False,
      "promotion_eligible":False,
      "limitations":["Training threshold selected using executed incumbent trades only",
                     "Holdout is chronological and not used for selection",
                     "Dropped observed trades are not a counterfactual portfolio simulation",
                     "No fills/fees/alternate position interactions reconstructed",
                     "No automatic change to Champion or real trading"]
    }

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
    result=explore_thresholds(rows)
    dst=Path(args.output);dst.parent.mkdir(parents=True,exist_ok=True)
    dst.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({"samples":result["sample_count"],"sufficient":result["sufficient_sample"],
                      "proposed_entry_score":result["recommended_entry_score"],
                      "train_observed_difference_sda":result["train_observed_difference_sda"],
                      "holdout_observed_difference_sda":result["holdout_observed_difference_sda"],
                      "promotion_eligible":False}))
if __name__=="__main__":main()
