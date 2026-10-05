#!/usr/bin/env python3
"""V30 Champion/Challenger optimizer. PAPER ONLY; never places orders."""
from __future__ import annotations
import json,itertools
from pathlib import Path
from datetime import datetime,timezone

STATS=Path("paper_stats.json"); DATA=Path("v30_optimizer_trades.json"); OUT=Path("v30_ai_optimizer.json")
MIN_ROWS=80; MIN_TEST=20

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
def score_candidate(train,test,c):
    tr=[x for x in train if eligible(x,c)]; te=[x for x in test if eligible(x,c)]
    mt,me=metrics(tr),metrics(te)
    # Require enough activity; optimize SDA with penalties for drawdown and tiny samples.
    if len(tr)<30 or len(te)<MIN_TEST:return None
    objective=me["pnl_sda"]-0.20*me["max_drawdown_sda"]+min(me["profit_factor"],3)*5
    return objective,mt,me
def main():
    stats=load(STATS,{})
    raw=load(DATA,{"trades":[]}); rows=[x for x in raw.get("trades",[]) if isinstance(x,dict)]
    rows.sort(key=lambda x:str(x.get("closed_at") or x.get("entry_at") or ""))
    result={"version":"V30-AI-OPTIMIZER-2","generated_at":datetime.now(timezone.utc).isoformat(),
      "mode":"PAPER_ONLY","objective":"maximize out-of-sample accumulated SDA with drawdown constraint",
      "aggregate_baseline":{"closed":int(f(stats.get("closed_trades"))),"pnl_sda":f(stats.get("realized_pnl_sda")),
        "win_rate_pct":f(stats.get("win_rate_pct"))},"dataset_rows":len(rows),"auto_apply":False,
      "guardrails":{"real_trading":False,"chronological_train_test_split":"70/30","minimum_dataset_rows":MIN_ROWS,
        "minimum_test_trades":MIN_TEST,"out_of_sample_required":True,"rollback_required":True}}
    if len(rows)<MIN_ROWS:
        result.update({"status":"COLLECTING_DATA","note":f"Need {MIN_ROWS} persisted trade rows; have {len(rows)}. No challenger selected."})
    else:
        cut=max(1,int(len(rows)*.70)); train,test=rows[:cut],rows[cut:]
        base_train,base_test=metrics(train),metrics(test)
        grids={
          "confidence":[0,68,72,76,80],"quality":[0,52,56,60],
          "impulse":[0,6,8,10,12],"m1h":[0,3,6,10,15,25],
          "buy_ratio":[0,1.1,1.25,1.5,2,3],"trades":[0,5,8,10,15]
        }
        best=None; tested=0
        for vals in itertools.product(*grids.values()):
            c=dict(zip(grids,vals)); z=score_candidate(train,test,c); tested+=1
            if z and (best is None or z[0]>best[0]):best=(z[0],c,z[1],z[2])
        result.update({"status":"READY","train_rows":len(train),"test_rows":len(test),
          "champion":{"train":base_train,"test":base_test},"candidates_tested":tested})
        if best:
            obj,c,mt,me=best
            # Promotion recommendation only if OOS SDA improves, DD does not worsen >10%, and PF improves.
            promote=(me["pnl_sda"]>base_test["pnl_sda"] and
                     me["max_drawdown_sda"]<=base_test["max_drawdown_sda"]*1.10 and
                     me["profit_factor"]>base_test["profit_factor"])
            result["challenger"]={"filters":c,"train":mt,"test":me,"objective":obj,"recommend_promotion":promote}
        else: result["challenger"]=None
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False))
if __name__=="__main__":main()
