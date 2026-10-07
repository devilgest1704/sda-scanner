#!/usr/bin/env python3
"""V30 Champion/Challenger optimizer. PAPER ONLY; never places orders."""
from __future__ import annotations
import json,itertools
from pathlib import Path
from datetime import datetime,timezone

STATS=Path("paper_stats.json"); DATA=Path("v30_optimizer_trades.json"); OUT=Path("v30_ai_optimizer.json"); CONFIG=Path("v30_agent_config.json")
MIN_ROWS=80; MIN_VALID=12; MIN_TEST=12
BASELINE={"entry_score":68.0,"quality":52.0,"impulse":8.0,"buy_ratio":1.10,"trades":5}

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
      (("confidence",),c["confidence"]),(("quality","pump_quality"),c["quality"]),(("impulse","pump_change"),c["impulse"]),
      (("buy_ratio",),c["buy_ratio"]),(("trades_1h","trades"),c["trades"])
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
    cfg=load(CONFIG,{"version":"V30-AUTO-CONFIG-1","generation":0,"filters":BASELINE.copy(),"previous_filters":None,"promotion":None})
    current=cfg.get("filters") if isinstance(cfg.get("filters"),dict) else BASELINE.copy()
    raw=load(DATA,{"trades":[]}); rows=[x for x in raw.get("trades",[]) if isinstance(x,dict)]
    rows.sort(key=lambda x:str(x.get("closed_at") or x.get("entry_at") or ""))
    # Optimize V30 only when enough V30-era rows exist; legacy schemas are not comparable.
    v30_rows=[x for x in rows if isinstance(em(x).get("v30"),dict) or em(x).get("pump_quality") is not None]
    analysis_rows=v30_rows if len(v30_rows)>=MIN_ROWS else rows
    result={"version":"V30-AI-OPTIMIZER-4","generated_at":datetime.now(timezone.utc).isoformat(),
      "mode":"PAPER_ONLY","runtime_gate_model":"V30.8 shared Champion gates; optimizer tunes only deployable runtime filters","objective":"maximize out-of-sample accumulated SDA with drawdown constraint",
      "aggregate_baseline":{"closed":int(f(stats.get("closed_trades"))),"pnl_sda":f(stats.get("realized_pnl_sda")),
        "win_rate_pct":f(stats.get("win_rate_pct"))},"dataset_rows":len(rows),"v30_compatible_rows":len(v30_rows),
      "analysis_scope":"V30_ONLY" if analysis_rows is v30_rows else "ALL_AVAILABLE_FALLBACK","auto_apply":True,"active_config":current,
      "guardrails":{"real_trading":False,"chronological_split":"60/20/20 train/validation/test","minimum_dataset_rows":MIN_ROWS,
        "minimum_validation_trades":MIN_VALID,"minimum_test_trades":MIN_TEST,"out_of_sample_required":True,"rollback_required":True}}
    if len(analysis_rows)<MIN_ROWS:
        result.update({"status":"COLLECTING_DATA","note":f"Need {MIN_ROWS} comparable trade rows; have {len(analysis_rows)}. No challenger selected."})
    else:
        cut1=max(1,int(len(analysis_rows)*.60)); cut2=max(cut1+1,int(len(analysis_rows)*.80))
        train,valid,test=analysis_rows[:cut1],analysis_rows[cut1:cut2],analysis_rows[cut2:]
        base_train,base_valid,base_test=metrics(train),metrics(valid),metrics(test)
        grids={
          "confidence":[0,68,72,76,80],"quality":[0,52,56,60],
          "impulse":[0,6,8,10,12],
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
                     me["profit_factor"]>base_test["profit_factor"] and
                     me["pnl_sda"]>0 and me["profit_factor"]>1.05 and mv["pnl_sda"]>0)
            safe_filters={
              "entry_score":max(BASELINE["entry_score"],c["confidence"] or BASELINE["entry_score"]),
              "quality":max(BASELINE["quality"],c["quality"] or BASELINE["quality"]),
              "impulse":max(BASELINE["impulse"],c["impulse"] or BASELINE["impulse"]),
              "buy_ratio":max(BASELINE["buy_ratio"],c["buy_ratio"] or BASELINE["buy_ratio"]),
              "trades":max(BASELINE["trades"],c["trades"] or BASELINE["trades"])
            }
            changed=any(abs(f(safe_filters[k])-f(current.get(k)))>1e-9 for k in safe_filters)
            result["challenger"]={"filters":c,"safe_runtime_filters":safe_filters,"train":mt,"validation":mv,"test":me,
              "objective_validation":obj,"recommend_promotion":promote and changed}
            if promote and changed:
                ts=datetime.now(timezone.utc).isoformat()
                cfg={"version":"V30-AUTO-CONFIG-1","generation":int(f(cfg.get("generation")))+1,
                     "updated_at":ts,"source":"AI_OUT_OF_SAMPLE_PROMOTION","filters":safe_filters,
                     "previous_filters":current,"promotion":{"at":ts,"dataset_rows":len(rows),
                     "v30_rows":len(v30_rows),"test":me,"baseline_test":base_test}}
                CONFIG.write_text(json.dumps(cfg,indent=2,ensure_ascii=False)+chr(10),encoding="utf-8")
                result["promotion_applied"]=True; result["active_config"]=safe_filters
            else: result["promotion_applied"]=False
        else: result["challenger"]=None

    # Roll back an automatically promoted Champion after enough new live paper
    # trades if it loses SDA or breaches 125% of its promotion-test drawdown.
    promo=cfg.get("promotion") if isinstance(cfg,dict) else None
    prev=cfg.get("previous_filters") if isinstance(cfg,dict) else None
    if promo and prev and promo.get("at"):
        post=[x for x in analysis_rows if str(x.get("closed_at") or "")>str(promo["at"])]
        if len(post)>=MIN_TEST:
            pm=metrics(post); limit=max(1.0,f((promo.get("test") or {}).get("max_drawdown_sda"))*1.25)
            if pm["pnl_sda"]<0 or pm["max_drawdown_sda"]>limit:
                ts=datetime.now(timezone.utc).isoformat()
                cfg={"version":"V30-AUTO-CONFIG-1","generation":int(f(cfg.get("generation")))+1,
                     "updated_at":ts,"source":"AUTO_ROLLBACK","filters":prev,
                     "previous_filters":None,"promotion":None}
                CONFIG.write_text(json.dumps(cfg,indent=2,ensure_ascii=False)+chr(10),encoding="utf-8")
                result["rollback_applied"]=True; result["rollback_metrics"]=pm; result["active_config"]=prev
            else:
                result["rollback_applied"]=False; result["post_promotion_metrics"]=pm
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+chr(10),encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False))
if __name__=="__main__":main()
