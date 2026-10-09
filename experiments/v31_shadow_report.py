"""Read-only V31 shadow report; mirrors V30 quality without touching state."""
import json
from pathlib import Path
from experiments.v31_profit_gate import evaluate

def quality_from_metrics(c):
    """Exact V30 quality components, independent of persisted impulse history."""
    momentum=max(0,min(28,c["m1"]*1.8+max(0,c["m15"])*1.5))
    flow_score=max(0,min(24,c["flow"]/400*8+c["flow15"]/200*5))
    pressure=max(0,min(18,(c["buy_ratio"]-1)*10))
    activity=max(0,min(14,(c["trades"]/10)*7+(c["vol"]/1000)*7))
    acceleration=max(0,min(10,c["accel"]/20+5))
    return momentum+flow_score+pressure+activity+acceleration

def run(path="market_analysis.json"):
    from v30_pump_hunter import metrics
    data=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data,dict):
        raise ValueError("Expected market_analysis.json object")
    tokens=data.get("tokens",data)
    if isinstance(tokens,dict):
        iterable=tokens.items()
    elif isinstance(tokens,list):
        iterable=((str(i),a) for i,a in enumerate(tokens))
    else:
        raise ValueError("Expected token mapping or list")
    results=[]
    for address,a in iterable:
        if not isinstance(a,dict): continue
        m=metrics(a)
        m["quality"]=quality_from_metrics(m)
        verdict=evaluate(m)
        results.append({"token":str(address),"eligible":verdict["eligible"],"quality":round(m["quality"],2),"reasons":verdict["reasons"]})
    return {"mode":"SHADOW_ONLY","evaluated":len(results),"eligible":sum(x["eligible"] for x in results),"results":results}

if __name__=="__main__":
    print(json.dumps(run(),ensure_ascii=False,indent=2))
