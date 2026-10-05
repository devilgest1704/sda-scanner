#!/usr/bin/env python3
"""V30 paper-only strategy optimizer.

Reads paper trade history/statistics and produces a challenger recommendation.
It NEVER places trades and NEVER rewrites the champion strategy automatically.
"""
from __future__ import annotations
import json, math
from pathlib import Path
from datetime import datetime, timezone

STATS=Path("paper_stats.json")
POSITIONS=Path("positions.json")
OUT=Path("v30_ai_optimizer.json")
MIN_CLOSED=50

def load(path, default):
    try:
        x=json.loads(path.read_text(encoding="utf-8"))
        return x
    except Exception:
        return default

def f(x,d=0.0):
    try:return float(x)
    except:return d

def trade_rows(p):
    if not isinstance(p,dict): return []
    for k in ("closed_trades","history","trades"):
        x=p.get(k)
        if isinstance(x,list): return [t for t in x if isinstance(t,dict)]
    return []

def pnl(t):
    for k in ("closed_profit_sda","profit_sda","pnl_sda","realized_pnl_sda"):
        if t.get(k) is not None:return f(t.get(k))
    return 0.0

def metrics(rows, stats):
    vals=[pnl(t) for t in rows]
    if not vals:
        n=int(f(stats.get("closed_trades")))
        return {
          "closed":n,"pnl_sda":f(stats.get("realized_pnl_sda")),
          "win_rate_pct":f(stats.get("win_rate_pct")),
          "profit_factor":None,"max_drawdown_sda":None
        }
    wins=sum(x for x in vals if x>0); losses=-sum(x for x in vals if x<0)
    eq=peak=0.0; dd=0.0
    for x in vals:
        eq+=x; peak=max(peak,eq); dd=max(dd,peak-eq)
    return {"closed":len(vals),"pnl_sda":sum(vals),
            "win_rate_pct":100*sum(x>0 for x in vals)/len(vals),
            "profit_factor":wins/losses if losses else None,
            "max_drawdown_sda":dd}

def entry_metrics(t):
    x=t.get("entry_metrics")
    return x if isinstance(x,dict) else {}

def loser_patterns(rows):
    losses=[t for t in rows if pnl(t)<0]
    wins=[t for t in rows if pnl(t)>0]
    keys=("confidence","quality","impulse","m15","m1h","m4h","net_1h","buy_ratio","trades_1h")
    out={}
    for k in keys:
        lv=[f(entry_metrics(t).get(k)) for t in losses if entry_metrics(t).get(k) is not None]
        wv=[f(entry_metrics(t).get(k)) for t in wins if entry_metrics(t).get(k) is not None]
        if lv and wv:
            out[k]={"loss_avg":sum(lv)/len(lv),"win_avg":sum(wv)/len(wv),
                    "delta_win_minus_loss":sum(wv)/len(wv)-sum(lv)/len(lv),
                    "loss_n":len(lv),"win_n":len(wv)}
    return out

def recommendations(base, patterns):
    rec=[]
    # Conservative challenger moves only; no loosening to manufacture BUYs.
    imp=patterns.get("impulse")
    qual=patterns.get("quality")
    conf=patterns.get("confidence")
    if imp and imp["win_avg"]>imp["loss_avg"]+1:
        rec.append({"parameter":"MIN_IMPULSE","direction":"raise",
                    "reason":"winning entries show materially stronger impulse"})
    if qual and qual["win_avg"]>qual["loss_avg"]+2:
        rec.append({"parameter":"MIN_QUALITY","direction":"raise",
                    "reason":"winning entries show materially higher quality"})
    if conf and conf["win_avg"]>conf["loss_avg"]+2:
        rec.append({"parameter":"ENTRY_SCORE","direction":"raise",
                    "reason":"winning entries show materially higher entry score"})
    if base["win_rate_pct"]<25:
        rec.append({"parameter":"ENTRY_SELECTIVITY","direction":"tighten",
                    "reason":"historical win rate is below 25%; prefer fewer higher-quality paper entries"})
    return rec

def main():
    stats=load(STATS,{})
    pos=load(POSITIONS,{})
    rows=trade_rows(pos)
    base=metrics(rows,stats)
    pats=loser_patterns(rows)
    enough=base["closed"]>=MIN_CLOSED
    result={
      "version":"V30-AI-OPTIMIZER-1",
      "generated_at":datetime.now(timezone.utc).isoformat(),
      "mode":"PAPER_ONLY",
      "objective":"maximize accumulated SDA subject to drawdown/stability constraints",
      "baseline":base,
      "sample_ok":enough,
      "patterns":pats,
      "challenger_recommendations":recommendations(base,pats) if enough else [],
      "auto_apply":False,
      "guardrails":{
        "real_trading":False,
        "minimum_closed_trades":MIN_CLOSED,
        "never_loosen_to_force_buys":True,
        "champion_requires_out_of_sample_validation":True,
        "rollback_required":True
      }
    }
    OUT.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=="__main__": main()
