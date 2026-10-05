#!/usr/bin/env python3
"""V30 Champion/Challenger optimizer. PAPER ONLY; never places orders."""
from __future__ import annotations
import json,itertools
from pathlib import Path
from datetime import datetime,timezone

STATS=Path("paper_stats.json"); DATA=Path("v30_optimizer_trades.json"); OUT=Path("v30_ai_optimizer.json")
MIN_ROWS=120; MIN_VALID=15; MIN_TEST=15

def load(p,d):
    try:return json.loads(p.read_text(encoding="utf-8"))
    except Exception:return d
def f(x,d=0.0):
    try:return float(x)
    except Exception:return d
def pnl(t): return f(t.get("closed_profit_sda"))
def em(t): return t.get("entry_metrics") if isinstance(t.get("entry_metrics"),dict) else {}
def val(t,*keys):
    m=em(t)
    for k in keys:
        if m.get(k) is not None:return f(m[k])
    return None
def metrics(rows):
    v=[pnl(x) for x in rows]; eq=peak=dd=0.; gp=sum(x for x in v if x>0); gl=-sum(x for x in v if x<0)
    for x in v: eq+=x; peak=max(peak,eq); dd=max(dd,peak-eq)
    return {"closed":len(v),"pnl_sda":sum(v),"win_rate_pct":100*sum(x>0 for x in v)/len(v) if v else 0,
            "profit_factor":gp/gl if gl else (999. if gp else 0.),"max_drawdown_sda":dd}
def eligible(t,c):
    checks=[
      (("confidence",),c["confidence"]),(("quality",),c["quality"]),(("impulse",),c["impulse"]),
      (("m1h","m1","m1h_pct"),c["m1h"]),(("buy_ratio",),c["buy_ratio"]),(("trades_1h","trades"),c["trades"])
    ]
    for keys,thr in checks:
        x=val(t,*keys)
        if thr>0 and (x is None or x<thr):return False
    return True
def score_candidate(train,valid,c):
    tr=[x for x in train if eligible(x,c)]; va=[x for x in valid if eligible(x,c)]
    mt,mv=metrics(tr),metrics(va)
    # Candidate selection uses TRAIN+VALIDATION only. Test remains untouched.
    if len(tr)<30 or len(va)<MIN_VALID:return None
    objective=mv["pnl_sda"]-0.20*mv["max_drawdown_sda"]+min(mv["profit_factor"],3)*5
    return objective,mt,mv
def main():
    stats=load(STATS,{})
    raw=load(DATA,{"trades":[]}); rows=[x for x in raw.get("trades",[]) if isinstance(x,dict)]
    rows.sort(key=lambda x:str(x.get("closed_at") or x.get("entry_at") or ""))
    result={"version":"V30-AI-OPTIMIZER-3","generated_at":datetime.now(timezone.utc).isoformat(),
      "mode":"PAPER_ONLY","objective":"maximize out-of-sample accumulated SDA with drawdown constraint",
      "aggregate_baseline":{"closed":int(f(stats.get("closed_trades"))),"pnl_sda":f(stats.get("realized_pnl_sda")),
        "win_rate_pct":f(stats.get("win_rate_pct"))},"dataset_rows":len(rows),"auto_apply":False,
      "guardrails":{"real_trading":False,"chronological_split":"60/20/20 train/validation/test","minimum_dataset_rows":MIN_ROWS,
        "minimum_validation_trades":MIN_VALID,"minimum_test_trades":MIN_TEST,"out_of_sample_required":True,"rollback_required":True}}
    if len(rows)<MIN_ROWS:
        result.update({"status":"COLLECTING_DATA","note":f"Need {MIN_ROWS} persisted trade rows; have {len(rows)}. No challenger selected."})
    else:
        cut1=max(1,int(len(rows)*.60)); cut2=max(cut1+1,int(len(rows)*.80))
        train,valid,test=rows[:cut1],rows[cut1:cut2],rows[cut2:]
        base_train,base_valid,base_test=metrics(train),metrics(valid),metrics(test)
        grids={
          "confidence":[0,68,72,76,80],"quality":[0,52,56,60],
          "impulse":[0,6,8,10,12],"m1h":[0,3,6,10,15,25],
          "buy_ratio":[0,1.1,1.25,1.5,2,3],"trades":[0,5,8,10,15]
        }
        best=None; tested=0
        for vals in itertools.product(*grids.values()):
            c=dict(zip(grids,vals)); z=score_candidate(train,valid,c); tested+=1
            if z and (best is None or z[0]>best[0]):best=(z[0],c,z[1],z[2])
        result.update({"status":"READY","train_rows":len(train),"validation_rows":len(valid),"test_rows":len(test),
          "champion":{"train":base_train,"validation":base_valid,"test":base_test},"candidates_tested":tested})
        if best:
            obj,c,mt,mv=best
            # Evaluate the validation-selected challenger exactly once on untouched TEST.
            test_filtered=[x for x in test if eligible(x,c)]
            me=metrics(test_filtered)
            promote=(len(test_filtered)>=MIN_TEST and
                     me["pnl_sda"]>base_test["pnl_sda"] and
                     me["max_drawdown_sda"]<=base_test["max_drawdown_sda"]*1.10 and
                     me["profit_factor"]>base_test["profit_factor"])
            result["challenger"]={"filters":c,"train":mt,"validation":mv,"test":me,
              "objective_validation":obj,"recommend_promotion":promote}
        else: result["challenger"]=None
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False))
if __name__=="__main__":main()
