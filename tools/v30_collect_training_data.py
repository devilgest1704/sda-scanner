#!/usr/bin/env python3
"""Persist deduplicated closed paper trades for V30 offline optimization."""
import json, hashlib
from pathlib import Path

SRC=Path("positions.json")
DST=Path("v30_optimizer_trades.json")
MAX_ROWS=5000

def load(p,d):
    try:return json.loads(p.read_text(encoding="utf-8"))
    except Exception:return d

def key(t):
    raw="|".join(str(t.get(k,"")) for k in ("address","label","entry_at","closed_at","close_reason","closed_profit_sda","closed_fraction"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def main():
    src=load(SRC,{})
    old=load(DST,{"trades":[]})
    rows=old.get("trades",[]) if isinstance(old,dict) else []
    by={str(x.get("_optimizer_id") or key(x)):x for x in rows if isinstance(x,dict)}
    for t in (src.get("closed_trades",[]) if isinstance(src,dict) else []):
        if not isinstance(t,dict):continue
        x=dict(t); x["_optimizer_id"]=key(x); by[x["_optimizer_id"]]=x
    out=list(by.values())[-MAX_ROWS:]
    DST.write_text(json.dumps({"schema":1,"trades":out},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"V30 optimizer dataset: {len(out)} closed trade rows")

if __name__=="__main__":main()
