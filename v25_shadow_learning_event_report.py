"""Event-level false-negative analysis for V25 shadow learning."""
from __future__ import annotations
import json, os, statistics, time
from collections import defaultdict
from typing import Any, Dict, List
STATE_FILE="v25_shadow_learning_v2.json"
REPORT_FILE="v25_shadow_learning_event_report.json"
HORIZONS=(5,10,20)
THRESHOLDS=(0.0,3.0,5.0,10.0)
MAX_SCAN_GAP=2
FEATURE_KEYS=("pump_score","score","volume","trades","m1h","m15","m4h","flow","accel","volume_ratio","trade_ratio","p5","p10","p20","p30")

def _load(path:str)->Dict[str,Any]:
    try:
        with open(path,"r",encoding="utf-8") as f: data=json.load(f)
        return data if isinstance(data,dict) else {}
    except Exception: return {}

def _num(v:Any)->float|None: return float(v) if isinstance(v,(int,float)) else None

def _scan(r:Dict[str,Any])->int|None:
    try: return int(r.get("scan"))
    except (TypeError,ValueError): return None

def _outcome(r:Dict[str,Any],h:int)->float|None:
    o=(r.get("outcomes") or {}).get(str(h))
    return _num(o.get("net_return_pct")) if isinstance(o,dict) else None

def _identity(r:Dict[str,Any])->str:
    for k in ("address","token","symbol","name"):
        if r.get(k): return str(r[k])
    return "UNKNOWN"

def _reason_group(reason:str)->str:
    s=str(reason).upper()
    if s.startswith("PUMP SCORE"): return "PUMP_SCORE"
    if s.startswith("VOLUME"): return "VOLUME"
    if s.startswith("TRADES"): return "TRADES"
    for g in ("M1H","M15","M4H"):
        if s.startswith(g): return g
    if s.startswith("FLOW"): return "FLOW"
    if "TECHNICAL BEAR" in s: return "TECHNICAL_BEAR"
    if "PUMP ACCEL" in s or "PRESSURE" in s: return "PUMP_ACCEL_PRESSURE"
    if "LEARNER EV" in s: return "LEARNER_EV"
    if "LEARNER DOWNSIDE" in s: return "LEARNER_DOWNSIDE"
    return "UNKNOWN"

def _groups(r:Dict[str,Any])->List[str]: return sorted({_reason_group(x) for x in (r.get("rejection_reasons") or [])})

def _features(r:Dict[str,Any])->Dict[str,float]:
    f=r.get("features") or {}
    return {k:round(v,3) for k in FEATURE_KEYS if (v:=_num(f.get(k))) is not None}

def _make_events(rows:List[Dict[str,Any]])->List[List[Dict[str,Any]]]:
    buckets:Dict[str,List[Dict[str,Any]]]=defaultdict(list)
    for r in rows:
        if isinstance(r,dict) and not r.get("allowed"): buckets[_identity(r)].append(r)
    events=[]
    for rs in buckets.values():
        rs.sort(key=lambda r:(_scan(r) if _scan(r) is not None else 10**12)); cur=[]; last=None
        for r in rs:
            s=_scan(r)
            if cur and (s is None or last is None or s-last>MAX_SCAN_GAP): events.append(cur); cur=[]
            cur.append(r); last=s
        if cur: events.append(cur)
    return sorted(events,key=lambda e:(_scan(e[0]) if _scan(e[0]) is not None else 10**12,_identity(e[0])))

def _event_value(e:List[Dict[str,Any]],h:int)->float|None:
    vals=[v for r in e if (v:=_outcome(r,h)) is not None]
    return max(vals) if vals else None

def _record(e:List[Dict[str,Any]])->Dict[str,Any]:
    first=e[0]; best={}
    for h in HORIZONS:
        v=_event_value(e,h)
        if v is not None: best[str(h)]=round(v,3)
    return {"identity":_identity(first),"first_scan":_scan(first),"last_scan":_scan(e[-1]),"snapshot_count":len(e),"best_net_return_pct":best,"first_rejection_groups":_groups(first),"event_rejection_groups":sorted({g for r in e for g in _groups(r)}),"first_features":_features(first)}

def _stats(events):
    completed=[(v,e) for e in events if (v:=_event_value(e,10)) is not None]
    out={"event_count":len(events),"completed_10h":len(completed)}
    for t in THRESHOLDS:
        vals=[v for v,_ in completed if v>t]; k=f"gt_{str(t).replace('.','_')}_pct"
        out[k]={"count":len(vals),"avg_net_return_pct":round(statistics.mean(vals),3) if vals else None,"median_net_return_pct":round(statistics.median(vals),3) if vals else None,"best_net_return_pct":round(max(vals),3) if vals else None}
    return out

def _group_stats(events):
    out={}; names=sorted({g for e in events for r in e for g in _groups(r)})
    for n in names:
        matched=[e for e in events if n in {g for r in e for g in _groups(r)}]; vals=[v for e in matched if (v:=_event_value(e,10)) is not None]; wins=[v for v in vals if v>0]
        out[n]={"events":len(matched),"completed_10h":len(vals),"wins_gt_0_pct":len(wins),"win_rate_pct":round(100*len(wins)/len(vals),2) if vals else None,"avg_best_10h_net_return_pct":round(statistics.mean(vals),3) if vals else None}
    return out

def _feature_means(records:List[Dict[str,Any]])->Dict[str,float]:
    out={}
    for k in FEATURE_KEYS:
        vals=[r["first_features"][k] for r in records if k in r.get("first_features",{})]
        if vals: out[k]=round(statistics.mean(vals),3)
    return out

def _positive_analysis(records:List[Dict[str,Any]])->Dict[str,Any]:
    positive=[r for r in records if r.get("best_net_return_pct",{}).get("10",-10**9)>0]
    completed=[r for r in records if "10" in r.get("best_net_return_pct",{})]
    negative=[r for r in completed if r["best_net_return_pct"]["10"]<=0]
    positive.sort(key=lambda r:r["best_net_return_pct"]["10"],reverse=True)
    groups=sorted({g for r in positive for g in r.get("event_rejection_groups",[])})
    group_hits={g:sum(1 for r in positive if g in r.get("event_rejection_groups",[])) for g in groups}
    return {
        "positive_event_count":len(positive),
        "positive_group_hit_rate_pct":{g:round(100*n/len(positive),1) for g,n in group_hits.items()} if positive else {},
        "positive_avg_first_features":_feature_means(positive),
        "negative_avg_first_features":_feature_means(negative),
        "completed_event_avg_first_features":_feature_means(completed),
        "top_positive_events":positive[:10],
    }

def build_report(data):
    events=_make_events(data.get("observations",[])); records=[_record(e) for e in events]
    top=[r for r in records if r.get("best_net_return_pct",{}).get("10",-10**9)>10]; top.sort(key=lambda r:r["best_net_return_pct"]["10"],reverse=True)
    return {"generated_at":time.time(),"source_observations":len(data.get("observations",[])),"deduplication":{"key":"address/token/symbol/name","max_scan_gap":MAX_SCAN_GAP},"stats":_stats(events),"rejection_group_stats":_group_stats(events),"positive_event_analysis":_positive_analysis(records),"top_unique_events_gt_10_pct":top[:10],"note":"Observational only. Event deduplication does not alter V25 gate decisions or MAX-WIN."}

def write_report():
    report=build_report(_load(STATE_FILE)); tmp=REPORT_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(report,f,ensure_ascii=False,indent=2,sort_keys=True)
    os.replace(tmp,REPORT_FILE); return report
if __name__=="__main__": write_report()
