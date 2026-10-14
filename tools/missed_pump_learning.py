"""Automatically learn *candidate* BUY rules from missed opportunities.

A row contains only the scanner features frozen BEFORE its later observed
quote. Outcomes are fixed 60-minute observed prices after modeled fees, never
a post-hoc peak fill, a real trade or a claim of achievable profit.

Only chronologically earlier training data select a rule. Chronological
validation is opened once for that fixed choice. The resulting research lane
is FORWARD tested independently and cannot promote PAPER Champion directly.
"""
import json
import math
from datetime import datetime, timedelta, timezone

SDA_NOTIONAL=50.0
FRICTION_PER_SIDE=.011
MIN_TRAIN=45
MIN_VALIDATION=18
MIN_SELECTED_TRAIN=14
MIN_SELECTED_VALIDATION=7
MIN_UNIQUE_TRAIN=7
MIN_UNIQUE_VALIDATION=4
OBSERVATION_COOLDOWN_SECONDS=4*3600

# All alternatives are bounded by the exact prospective research entry gate.
# The training algorithm changes only ONE factor at a time, with no ability to
# remove fresh quotes, count, flow or trend anti-chase safety requirements.
PARAMETERS=(
    ("flow_15m","min",(100,200,350,500)),
    ("m15","min",(.8,1.5,2.5)),
    ("buy_ratio","min",(1.5,2.0,3.0)),
    ("trades_1h","min",(6,8,10,12)),
    ("m4h","max",(8,15,22,30)),
    ("m1h","max",(4,6,8)),
    ("volume_1h","min",(650,850,1200)),
    ("quality","min",(56,64,72)),
)


def number(value):
    if isinstance(value,bool):
        return None
    try:
        x=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    return x if math.isfinite(x) else None


def at(value):
    if not isinstance(value,str):
        return None
    try:
        time=datetime.fromisoformat(value.replace("Z","+00:00"))
        return time.astimezone(timezone.utc) if time.tzinfo else None
    except (TypeError,ValueError,OverflowError):
        return None


def net_sda(entry,exit_price):
    return SDA_NOTIONAL*((exit_price*(1-FRICTION_PER_SIDE))/
                        (entry*(1+FRICTION_PER_SIDE))-1)


def normalize(observations):
    """Select causally timestamped missed entries incl. negative outcomes.

    Require a genuine fresh quote at candidate time, a verified later observed
    quote within 15m of the 60m horizon and at most one correlated candidate
    per token per four hours. Older schema rows without frozen features are
    ignored rather than reconstructed with hindsight.
    """
    valid=[]
    for row in observations if isinstance(observations,list) else []:
        if not isinstance(row,dict) or row.get("eligible") is True:
            continue
        t=at(row.get("at"))
        quote=at(row.get("quote_at"))
        f=row.get("features")
        outcome=(row.get("outcomes") or {}).get("60m")
        if (t is None or quote is None or not isinstance(f,dict)
                or not isinstance(outcome,dict)
                or not isinstance(row.get("gates"),list)
                or not row["gates"]):
            continue
        age=(t-quote).total_seconds()
        future=at(outcome.get("observed_at"))
        entry=number(row.get("entry_price_sda"))
        end_price=number(outcome.get("price_sda"))
        if (not 0<=age<=300 or future is None or future<=t
                or not 60<=((future-t).total_seconds()/60)<=75
                or entry is None or entry<=0
                or end_price is None or end_price<=0):
            continue
        fields=("score","quality","snapshot_impulse","m1h","m15","m4h",
                "trades_1h","buy_ratio","volume_1h","flow_1h","flow_15m")
        values={k:number(f.get(k)) for k in fields}
        if any(v is None for v in values.values()):
            continue
        address=str(row.get("address") or "").lower()
        if not address:
            continue
        valid.append({"at":t,"address":address,"features":values,
                      "pnl":net_sda(entry,end_price),
                      "gates":tuple(row["gates"]),
                      "observed_peak_pct":number((row.get("observed_peak_240m") or {}).get(
                          "price_return_pct"))})
    valid.sort(key=lambda x:(x["at"],x["address"]))
    latest={}
    distinct=[]
    for row in valid:
        previous=latest.get(row["address"])
        if previous and (row["at"]-previous).total_seconds()<OBSERVATION_COOLDOWN_SECONDS:
            continue
        latest[row["address"]]=row["at"]
        distinct.append(row)
    return distinct


def safe_missed_entry(row,policy):
    """Immutable minimum risk conditions for a new forward Shadow BUY filter.

    Never applied to real orders or the PAPER Champion. Dynamic policy cannot
    reduce floors, remove anti-chase caps, or bypass the global loss circuit.
    """
    if not isinstance(row,dict) or not isinstance(policy,dict):
        return False
    if policy.get("entry_lane")!="missed_pump":
        return False
    if row.get("phase") not in ("WATCH","IGNITION","CONFIRMATION"):
        return False
    fields=("score","quality","snapshot_impulse","m1h","m15","m4h",
            "trades_1h","buy_ratio","volume_1h","flow_1h","flow_15m",
            "data_age_sec","price_sda")
    v={k:number(row.get(k)) for k in fields}
    if any(x is None for x in v.values()):
        return False
    if not(
        0<=v["data_age_sec"]<=180 and v["price_sda"]>0
        and v["score"]>=max(60,number(policy.get("entry_score")) or 68)
        and v["quality"]>=max(56,number(policy.get("quality")) or 56)
        and v["snapshot_impulse"]>=max(8,number(policy.get("impulse")) or 8)
        and 0<v["m1h"]<=8 and .8<=v["m15"]<=8
        and -5<=v["m4h"]<=30
        and v["trades_1h"]>=max(6,number(policy.get("trades")) or 6)
        and v["buy_ratio"]>=max(1.5,number(policy.get("buy_ratio")) or 1.5)
        and v["volume_1h"]>=650 and v["flow_1h"]>=300
        and v["flow_15m"]>=100
    ):
        return False
    k=policy.get("learned_feature")
    if k is None:
        return True
    mode=policy.get("learned_mode")
    bound=number(policy.get("learned_bound"))
    if bound is None or (k,mode) not in {(x,y) for x,y,_ in PARAMETERS}:
        return False
    val=v[k]
    return val>=bound if mode=="min" else val<=bound


def summary(rows):
    result=[r["pnl"] for r in rows]
    gross_win=sum(x for x in result if x>0)
    gross_loss=-sum(x for x in result if x<0)
    pnl=round(sum(result),5)
    peak=equity=dd=0
    for gain in result:
        equity+=gain
        peak=max(peak,equity)
        dd=max(dd,peak-equity)
    return {"samples":len(result),
            "unique_tokens":len({r["address"] for r in rows}),
            "net_sda_model":pnl,
            "mean_sda_model":round(pnl/len(result),5) if result else 0,
            "profit_factor_model":round(gross_win/gross_loss,4)
                if gross_loss else (99 if gross_win else 0),
            "max_drawdown_sda_model":round(dd,4),
            "win_rate_pct_model":round(100*sum(x>0 for x in result)/len(result),2)
                if result else 0}


def learn(observations):
    all_rows=normalize(observations)
    # Establish a deterministic chronological cut BEFORE examining profits.
    cut=int(len(all_rows)*.70)
    train,valid=all_rows[:cut],all_rows[cut:]
    # Purge the boundary so the last train label (T+60..75m) is known
    # BEFORE the first unseen holdout entry timestamp. This stops subtle
    # 60-minute lookahead at the train/validation frontier.
    if valid:
        earliest=valid[0]["at"]-timedelta(minutes=75)
        train=[r for r in train if r["at"]<=earliest]
    def rejection_counts(rows):
        counts={}
        for item in rows:
            for gate in set(item["gates"]):
                label=str(gate)
                counts[label]=counts.get(label,0)+1
        return dict(sorted(counts.items(),key=lambda x:(-x[1],x[0]))[:12])
    positives=[r for r in all_rows if r["pnl"]>=3]
    negatives=[r for r in all_rows if r["pnl"]<0]
    base={
        "mode":"OBSERVED_QUOTE_HYPOTHESIS_NO_EXECUTION_PARITY",
        "paper_only":True,"can_promote_champion":False,
        "sample_count":len(all_rows),
        "missed_pump_examples":len(positives),
        "positive_theoretical_60m_sda_model_not_fills":round(
            sum(r["pnl"] for r in positives),4),
        "negative_theoretical_60m_sda_model_not_fills":round(
            sum(r["pnl"] for r in negatives),4),
        "gates_on_profitable_rejected_candidates":rejection_counts(positives),
        "gates_on_loss_making_rejected_candidates":rejection_counts(negatives),
        "potential_240m_peak_examples":sum(
            r["observed_peak_pct"] is not None and r["observed_peak_pct"]>=10
            for r in all_rows),
        "objective":"net SDA at first 60m observed quote after 2.2% roundtrip model cost",
        "limitation":"no fill or intrabar peak assumed; 4h/token de-correlation; new rule requires untouched forward shadow",
        "train":summary(train),"validation_sample_count":len(valid),
        "status":"COLLECTING","candidate":None,
    }
    if (len(train)<MIN_TRAIN or len(valid)<MIN_VALIDATION
            or len({r["address"] for r in train})<MIN_UNIQUE_TRAIN
            or len({r["address"] for r in valid})<MIN_UNIQUE_VALIDATION):
        return base
    safe={"entry_lane":"missed_pump","entry_score":60,
          "quality":56,"impulse":8,"buy_ratio":1.5,"trades":6}
    def selected(data,policy):
        return [r for r in data if safe_missed_entry({
            **r["features"],"data_age_sec":30,"price_sda":1.0,
            "phase":"WATCH"},policy)]
    baseline_train=selected(train,safe)
    baseline_valid=selected(valid,safe)
    base["eligible_unmodified_train"]=summary(baseline_train)
    base["eligible_unmodified_validation"]=summary(baseline_valid)
    proposals=[]
    for key,mode,thresholds in PARAMETERS:
        for bound in thresholds:
            policy={**safe,"learned_feature":key,
                    "learned_mode":mode,"learned_bound":bound}
            t=selected(train,policy)
            if (len(t)<MIN_SELECTED_TRAIN
                    or len({x["address"] for x in t})<MIN_UNIQUE_TRAIN):
                continue
            stats=summary(t)
            # Reject empty or negative expected-value selection and demand
            # material improvement over an unfiltered safe cohort.
            if (stats["net_sda_model"]<=0
                    or stats["profit_factor_model"]<=1.12
                    or stats["mean_sda_model"]<=
                        summary(baseline_train)["mean_sda_model"]+.30):
                continue
            objective=stats["mean_sda_model"]-.04*stats["max_drawdown_sda_model"]
            proposals.append((objective,key,mode,bound,policy,stats))
    if not proposals:
        return {**base,"status":"NO_PROFITABLE_TRAIN_RULE",
                "tested_rules":sum(len(z) for _,_,z in PARAMETERS)}
    proposals.sort(key=lambda x:(-x[0],x[1],x[3]))
    _,key,mode,bound,policy,tstat=proposals[0]
    # HOLDOUT is assessed ONCE for this single train-selected policy.
    v=selected(valid,policy)
    vstat=summary(v)
    passed=(vstat["samples"]>=MIN_SELECTED_VALIDATION
            and vstat["unique_tokens"]>=MIN_UNIQUE_VALIDATION
            and vstat["net_sda_model"]>0
            and vstat["profit_factor_model"]>1.10
            and vstat["mean_sda_model"]>
                summary(baseline_valid)["mean_sda_model"]+.15)
    candidate={"hypothesis":f"missed_60m_net_sda:{key}:{mode}:{bound}",
               "policy":policy,"train":tstat,"validation":vstat,
               "selection":"single feature train-only search, untouched chronological validation",
               "selection_bias_warning":"Outcome is a quoted 60m mark, NOT an executed trade. Verify on future Shadow portfolio.",
               "evidence_tier":"CHRONOLOGICAL_QUOTE_VALIDATION" if passed else "REJECTED_VALIDATION",
               "can_promote_paper":False,"can_trade_real":False}
    return {**base,"status":"VALIDATED_FOR_FORWARD_SHADOW" if passed
            else "TRAIN_RULE_FAILED_VALIDATION",
            "tested_rules":sum(len(z) for _,_,z in PARAMETERS),
            "candidate":candidate}


def main():
    from pathlib import Path
    source=Path("strategy_lab/candidate_outcomes.json")
    output=Path("strategy_lab/missed_pump_learning.json")
    raw=json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    if not isinstance(raw,dict):
        raise ValueError("Candidate outcomes must be a JSON object")
    report=learn(raw.get("observations",[]))
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    temp.replace(output)
    print("MISSED PUMP AI LEARNER:",json.dumps({
        "status":report["status"],"observations":report["sample_count"],
        "observed_60m_positive":report["missed_pump_examples"],
        "proposed_rule":(report["candidate"] or {}).get("hypothesis")}))


if __name__=="__main__":
    main()
