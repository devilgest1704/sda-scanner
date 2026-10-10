#!/usr/bin/env python3
"""Export completed paper trades into a deduplicated research dataset.

Run on the same runner/filesystem as the paper engine, AFTER positions.json
has been written. Never modifies positions or the active strategy.
"""
import argparse
import json
from pathlib import Path

def export(source,destination):
    raw=json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(raw,dict): raise ValueError("positions state must be an object")
    rows=raw.get("closed_trades",[])
    if not isinstance(rows,list): raise ValueError("closed_trades must be a list")
    path=Path(destination)
    previous=[]
    if path.exists():
        previous=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous,list): raise ValueError("archive must be a list")
    merged={}
    for row in previous+rows:
        if not isinstance(row,dict) or str(row.get("status","")).upper()!="CLOSED": continue
        address=str(row.get("address","")).lower()
        opened=str(row.get("opened_at",""))
        closed=str(row.get("closed_at",""))
        if not (address and opened and closed): continue
        key=(address,opened,closed)
        merged[key]=row
    out=[merged[k] for k in sorted(merged)]
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(out,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return len(out),len(rows)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--positions",default="positions.json")
    ap.add_argument("--archive",default="strategy_lab/closed_trades.json")
    args=ap.parse_args()
    total,seen=export(args.positions,args.archive)
    print(f"Archive closed trades: {total} (source rows: {seen})")
