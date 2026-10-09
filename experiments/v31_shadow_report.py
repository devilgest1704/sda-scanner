"""Read-only V31 shadow evaluation against current market_analysis.json."""
import json
from pathlib import Path
from experiments.v31_profit_gate import evaluate

def run(path="market_analysis.json"):
    data=json.loads(Path(path).read_text(encoding="utf-8"))
    tokens=data.get("tokens",data) if isinstance(data,dict) else {}
    results=[]
    iterable=tokens.items() if isinstance(tokens,dict) else enumerate(tokens)
    for address,a in iterable:
        if not isinstance(a,dict): continue
        from v30_pump_hunter import metrics
        m=metrics(a)
        # Current V30's 'm1' is 1h momentum, not 1-minute momentum.
        # Quality is derived from current scoring, but score() can mutate state;
        # avoid it in a read-only shadow report.
        m["quality"]=None
        verdict=evaluate(m)
        results.append({"token":str(address),"eligible":verdict["eligible"],"reasons":verdict["reasons"]})
    return {"mode":"SHADOW_ONLY","evaluated":len(results),"eligible":sum(x["eligible"] for x in results),"results":results}

if __name__=="__main__":
    print(json.dumps(run(),ensure_ascii=False,indent=2))
