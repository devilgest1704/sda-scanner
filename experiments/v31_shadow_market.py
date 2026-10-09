"""Read-only V31.1 candidate evaluation on market_analysis.json."""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

def parse_time(value):
    if not isinstance(value,str): return None
    try:
        dt=datetime.fromisoformat(value.replace("Z","+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError: return None

def evaluate_token(token,asof):
    reasons=[]
    transaction=parse_time(token.get("last_transaction"))
    age=(asof-transaction).total_seconds() if transaction else None
    if age is None or not math.isfinite(age) or age<0: reasons.append("AGE_MISSING_OR_INVALID")
    elif age>300: reasons.append("AGE_STALE")
    flow=token.get("flow") or {}
    hour=flow.get("1h") if isinstance(flow,dict) else None
    hour=hour if isinstance(hour,dict) else {}
    buy,sell=hour.get("buy_count"),hour.get("sell_count")
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 for x in (buy,sell)):
        trades=None
        reasons.append("TRADES_MISSING_OR_INVALID")
    else:
        trades=buy+sell
        if trades<12: reasons.append("LOW_ACTIVITY")
    return {"eligible":not reasons,"reasons":reasons,"age_sec":round(age,1) if age is not None else None,"trades_1h":trades}

def run(data):
    asof=parse_time(data.get("updated_at"))
    if asof is None: raise ValueError("market_analysis.json missing valid updated_at; refuse evaluation")
    tokens=data.get("tokens")
    if not isinstance(tokens,dict): raise ValueError("market_analysis.json missing token mapping")
    results=[]
    for address,token in tokens.items():
        if isinstance(token,dict): results.append({"token":address,**evaluate_token(token,asof)})
    return {"mode":"SHADOW_ONLY","asof":asof.isoformat(),"evaluated":len(results),"eligible":sum(r["eligible"] for r in results),"results":results}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("market_file",nargs="?",default="market_analysis.json")
    p.add_argument("--output")
    p.add_argument("--append-log")
    args=p.parse_args()
    report=run(json.loads(Path(args.market_file).read_text(encoding="utf-8")))
    if args.output: Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if args.append_log:
        record={"asof":report["asof"],"evaluated":report["evaluated"],"eligible":report["eligible"],"eligible_tokens":[r["token"] for r in report["results"] if r["eligible"]]}
        with Path(args.append_log).open("a",encoding="utf-8") as f: f.write(json.dumps(record,ensure_ascii=False)+"\n")
    print(json.dumps({"mode":report["mode"],"asof":report["asof"],"evaluated":report["evaluated"],"eligible":report["eligible"]},ensure_ascii=False))

if __name__=="__main__": main()
