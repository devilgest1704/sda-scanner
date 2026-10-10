#!/usr/bin/env python3
"""Autonomous PAPER shadow optimizer: deterministic proposals, never alters live filters."""
import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, parse

FILTERS=("entry_score","quality","impulse","buy_ratio","trades")
BOUNDS={"entry_score":(68,85,2),"quality":(52,70,2),"impulse":(8,15,1),
        "buy_ratio":(1.10,1.60,0.10),"trades":(5,12,1)}

def load(path):
    with open(path,encoding="utf-8") as f: return json.load(f)

def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def analyze(stats,execution,config,previous=None):
    if execution.get("paper_only") is not True: raise ValueError("Only paper execution is allowed")
    base=config.get("filters",{})
    if any(not finite(base.get(k)) for k in FILTERS): raise ValueError("Invalid baseline")
    pnl=stats.get("cumulative_pnl_sda")
    if not finite(pnl): raise ValueError("Invalid P/L")
    events=execution.get("events",[])
    if not isinstance(events,list): raise ValueError("Invalid execution events")
    ready=[e for e in events if isinstance(e,dict) and e.get("event")=="READY"]
    created=[e for e in events if isinstance(e,dict) and e.get("event")=="CREATED"]
    # Rotate a bounded single-parameter hypothesis each day. This is a
    # proposal, NOT learning from outcomes or a validated optimization.
    day=datetime.now(timezone.utc).date().toordinal()
    key=FILTERS[day%len(FILTERS)]
    lo,hi,step=BOUNDS[key]
    candidate=dict(base)
    candidate[key]=round(min(hi,max(lo,base[key]+step)),3)
    result={
      "generated_at":datetime.now(timezone.utc).isoformat(),
      "mode":"paper_shadow_proposal_only",
      "production_mutated":False,
      "promotion_eligible":False,
      "reason":"No timestamped, joined closed-trade market snapshots for out-of-sample replay",
      "baseline":{"filters":base,"cumulative_pnl_sda":pnl,
          "closed_trades":stats.get("closed_trades"),"win_rate_pct":stats.get("win_rate_pct")},
      "observations":{"ready_events":len(ready),"created_events":len(created),
          "events_retained":len(events)},
      "challenger":{"filters":candidate,"status":"UNVALIDATED",
          "changed_filter":key,"hypothesis":"Test stricter "+key+" without touching Champion"},
      "promotion_requirements":["Immutable entry/exit fills and historical market snapshots",
          "No-lookahead walk-forward replay with fees and slippage",
          "Out-of-sample net SDA improvement and bounded drawdown",
          "Shadow observation followed by automatic rollback guard"]
    }
    if isinstance(previous,dict):
        old=previous.get("baseline",{})
        oldp=old.get("cumulative_pnl_sda")
        oldn=old.get("closed_trades")
        n=stats.get("closed_trades")
        if finite(oldp) and finite(oldn) and finite(n) and n>=oldn:
            result["since_previous_report"]={"new_closed_trades":n-oldn,
                "net_pnl_change_sda":round(pnl-oldp,4)}
    return result

def render_png(report,path):
    from PIL import Image,ImageDraw,ImageFont
    im=Image.new("RGB",(1000,610),"#101827")
    d=ImageDraw.Draw(im)
    try:
        font="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        f=lambda n,b=False:ImageFont.truetype(bold if b else font,n)
    except OSError:
        f=lambda n,b=False:ImageFont.load_default()
    d.text((48,32),"SDA  |  AI STRATEGY LAB",fill="#F3F6FA",font=f(37,True))
    d.text((50,95),"PAPER ONLY  /  SHADOW  /  NOT PROMOTED",fill="#8DA9C7",font=f(21))
    pnl=report["baseline"]["cumulative_pnl_sda"]
    d.rounded_rectangle((45,145,955,310),radius=18,fill="#1D2B40")
    d.text((72,165),"CUMULATIVE NET P/L",fill="#B7C9DD",font=f(20))
    d.text((72,210),f"{pnl:+,.2f} SDA",fill="#F47E82" if pnl<0 else "#70D6A0",font=f(48,True))
    d.text((52,348),f"Closed trades: {report['baseline']['closed_trades']}",fill="#F3F6FA",font=f(24))
    d.text((52,391),f"Win rate: {report['baseline']['win_rate_pct']}%",fill="#F3F6FA",font=f(24))
    c=report["challenger"]
    d.text((52,451),f"CHALLENGER: {c['changed_filter']}",fill="#8BC5F6",font=f(25,True))
    d.text((52,490),f"Proposed: {c['filters'][c['changed_filter']]}  |  UNVALIDATED",fill="#E5EBF4",font=f(22))
    series=report.get("equity_curve_sda") or []
    if len(series)>=2:
        vals=[float(v) for v in series]
        low=min(0,min(vals)); high=max(0,max(vals))
        span=max(1,high-low)
        coords=[(560+i*385/(len(vals)-1),425-(v-low)/span*95) for i,v in enumerate(vals)]
        d.line(coords,fill="#70D6A0" if vals[-1]>=0 else "#F47E82",width=4)
        d.text((570,323),"RECENT CLOSED-TRADE P/L",fill="#A8BDD4",font=f(17))
    d.text((52,561),"Auto-report active. No live filter changes.",fill="#AAB8C9",font=f(19))
    im.save(path,"PNG")

def telegram(report,png):
    token=os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat=os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or os.environ.get("CHAT_ID")
    if not token or not chat:
        print("Telegram skipped: bot token or chat id not configured")
        return False
    # Telegram Bot API sendPhoto multipart/form-data, no extra dependencies.
    import uuid
    boundary="----StrategyLab"+uuid.uuid4().hex
    def field(name,data,filename=None,ctype=None):
        head=f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
        if filename: head+=f'; filename="{filename}"'
        head+="\r\n"
        if ctype: head+=f"Content-Type: {ctype}\r\n"
        return (head+"\r\n").encode()+data+b"\r\n"
    caption=("AI Strategy Lab | PAPER SHADOW\n"
        f"Net P/L: {report['baseline']['cumulative_pnl_sda']:+.2f} SDA\n"
        f"Challenger: {report['challenger']['changed_filter']} "
        f"= {report['challenger']['filters'][report['challenger']['changed_filter']]}\n"
        "NOT VALIDATED / NOT PROMOTED")
    body=(field("chat_id",chat.encode())+field("caption",caption.encode())+
          field("photo",Path(png).read_bytes(),"strategy_lab.png","image/png")+
          f"--{boundary}--\r\n".encode())
    req=request.Request("https://api.telegram.org/bot"+token+"/sendPhoto",data=body,
        headers={"Content-Type":"multipart/form-data; boundary="+boundary},method="POST")
    with request.urlopen(req,timeout=25) as resp:
        data=json.load(resp)
    if not data.get("ok"): raise RuntimeError("Telegram rejected photo")
    return True

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--stats",default="paper_stats.json")
    ap.add_argument("--execution",default="v30_execution_state.json")
    ap.add_argument("--config",default="v30_agent_config.json")
    ap.add_argument("--previous",default="")
    ap.add_argument("--output",default="strategy_lab/report.json")
    ap.add_argument("--png",default="strategy_lab/status.png")
    ap.add_argument("--telegram",action="store_true")
    args=ap.parse_args()
    previous=load(args.previous) if args.previous and Path(args.previous).exists() else None
    report=analyze(load(args.stats),load(args.execution),load(args.config),previous)
    archive=Path("strategy_lab/closed_trades.json")
    if archive.exists():
        rows=load(archive)
        if isinstance(rows,list):
            rows=sorted((r for r in rows if isinstance(r,dict) and finite(r.get("closed_profit_sda"))),
                key=lambda r:str(r.get("closed_at","")))
            equity=[0.0]
            for row in rows[-100:]:
                equity.append(round(equity[-1]+row["closed_profit_sda"],4))
            report["equity_curve_sda"]=equity if len(equity)>1 else []
            report["archived_closed_trades"]=len(rows)
    for p in (args.output,args.png): Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    render_png(report,args.png)
    if args.telegram: telegram(report,args.png)
    print(json.dumps({"mode":report["mode"],"promotion_eligible":False,
        "challenger":report["challenger"],"telegram_requested":args.telegram}))
if __name__=="__main__": main()
