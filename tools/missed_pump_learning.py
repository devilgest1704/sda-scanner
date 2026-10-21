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
        # Preserve the ORIGINAL lane and entry price/quote age. Historical
        # simulations must use the exact gates the prospective Shadow arm
        # will apply, not an invented WATCH phase or 30s fresh quote.
        phase=f.get("phase")
        if phase not in ("WATCH","IGNITION","CONFIRMATION","BREAKOUT"):
            continue
        frozen_snapshot={
            **values,"phase":phase,"data_age_sec":age,"price_sda":entry,
        }
        address=str(row.get("address") or "").lower()
        if not address:
            continue
        valid.append({"at":t,"address":address,"features":values,
                      "rejection_reasons":{lane:row.get(field) for lane,field in (("temporal_recovery","temporal_recovery_reason"),("confirmed_continuation","continuation_reason")) if isinstance(row.get(field),str) and row.get(field)},
                      "frozen_snapshot":frozen_snapshot,
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
    # Allow a bounded conjunction of TWO pre-registered, train-selected
    # signals. An unrecognized or duplicated feature fails closed.
    rules=policy.get("learned_rules")
    if rules is None:
        k=policy.get("learned_feature")
        if k is None:
            return True
        rules=[{"feature":k,"mode":policy.get("learned_mode"),
                "bound":policy.get("learned_bound")}]
    if not isinstance(rules,list) or not 1<=len(rules)<=2:
        return False
    allowed={(x,y) for x,y,_ in PARAMETERS}
    seen=set()
    for rule in rules:
        if not isinstance(rule,dict):
            return False
        key=rule.get("feature")
        mode=rule.get("mode")
        bound=number(rule.get("bound"))
        if (not isinstance(key,str) or (key,mode) not in allowed
                or key in seen or bound is None):
            return False
        seen.add(key)
        if not (v[key]>=bound if mode=="min" else v[key]<=bound):
            return False
    return True


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



def archive_quality_diagnostics(observations):
    """Count where historical observations fail strict learning eligibility.

    This is read-only instrumentation, NOT a reason to relax stale-quote or
    timestamp safety gates. Keep distinct counts to expose incompatible
    outcome collection and older schema without synthesizing profit.
    """
    reasons={}
    missing_fields={}
    legacy_frozen_age_recoverable = 0
    original_schema_examples = []
    stages={
        "with_original_quote_and_features":0,
        "with_fresh_entry_quote":0,
        "with_strict_60_to_75m_exit_quote":0,
        "with_strict_exit_and_full_features":0,
    }
    latest_candidate_at=None
    latest_matured_at=None
    fields=("score","quality","snapshot_impulse","m1h","m15","m4h",
            "trades_1h","buy_ratio","volume_1h","flow_1h","flow_15m")
    def flag(reason):
        reasons[reason]=reasons.get(reason,0)+1
    for row in observations if isinstance(observations,list) else []:
        if not isinstance(row,dict):
            flag("invalid_archive_row")
            continue
        if row.get("eligible") is True:
            flag("already_eligible_not_missed")
            continue
        t=at(row.get("at"))
        quote=at(row.get("quote_at"))
        f=row.get("features")
        outcome=(row.get("outcomes") or {}).get("60m")
        if t is not None and (latest_candidate_at is None or t>latest_candidate_at):
            latest_candidate_at=t
        if t is None:
            flag("invalid_or_missing_candidate_time")
        if quote is None:
            flag("invalid_or_missing_quote_time")
            # Diagnosis only: a frozen original age may permit recovery
            # without reading any price recorded after the decision.
            observed_age = number(f.get("data_age_sec")) if isinstance(f,dict) else None
            if t is not None and observed_age is not None and 0 <= observed_age <= 300:
                legacy_frozen_age_recoverable += 1
            if len(original_schema_examples) < 3:
                original_schema_examples.append({
                    "row_keys":sorted(row.keys()),
                    "feature_keys": sorted(f) if isinstance(f,dict) else [],
                    "has_60m":isinstance(outcome,dict),
                    "has_original_age":observed_age is not None,
                })
        if isinstance(f,dict):
            for key in fields:
                if number(f.get(key)) is None:
                    missing_fields[key]=missing_fields.get(key,0)+1
        else:
            flag("missing_frozen_features")
        if not isinstance(row.get("gates"),list) or not row["gates"]:
            flag("missing_rejection_gates")
        if t is None or quote is None:
            continue
        if isinstance(f,dict):
            stages["with_original_quote_and_features"]+=1
        age=(t-quote).total_seconds()
        if not 0 <= age <= 300:
            flag("stale_entry_quote")
        else:
            stages["with_fresh_entry_quote"]+=1
        if not isinstance(outcome,dict):
            flag("no_60m_followup_observed")
            continue
        future=at(outcome.get("observed_at"))
        entry=number(row.get("entry_price_sda"))
        end_price=number(outcome.get("price_sda"))
        if future is None or future <= t:
            flag("invalid_followup_time")
        else:
            elapsed=(future-t).total_seconds()/60
            if 60 <= elapsed <=75:
                stages["with_strict_60_to_75m_exit_quote"]+=1
                if (0<=age<=300 and isinstance(f,dict)
                        and all(number(f.get(k)) is not None for k in fields)
                        and isinstance(row.get("gates"),list)
                        and bool(row["gates"])
                        and entry is not None and entry>0
                        and end_price is not None and end_price>0):
                    stages["with_strict_exit_and_full_features"]+=1
                    if latest_matured_at is None or t>latest_matured_at:
                        latest_matured_at=t
            elif elapsed < 60:
                flag("early_followup_mislabeled")
            elif elapsed > 75 and elapsed <=120:
                flag("late_followup_75_to_120_minutes")
            elif elapsed>120:
                flag("very_late_followup")
        if entry is None or entry<=0 or end_price is None or end_price<=0:
            flag("invalid_price")
    return {
        "rejection_counts":dict(sorted(reasons.items(),key=lambda x:(-x[1],x[0]))),
        "missing_feature_counts":dict(sorted(missing_fields.items(),key=lambda x:(-x[1],x[0]))),
        "legacy_frozen_age_recoverable":legacy_frozen_age_recoverable,
        "maturity_stages":stages,
        "latest_original_candidate_at":latest_candidate_at.isoformat() if latest_candidate_at else None,
        "latest_strict_matured_candidate_at":latest_matured_at.isoformat() if latest_matured_at else None,
        "missing_quote_schema_examples":original_schema_examples,
    }



def observed_peak_diagnostics(observations):
    """What the scanner actually missed, WITHOUT pretending to sell at peaks.

    A strictly frozen rejected decision is joined only to an independently
    recorded genuine swap within four hours. Keep the 60m executable-proxy
    target separate; highs must NEVER authorize a BUY or promotion.
    Four-hour de-correlation prevents one asset from becoming many successes.
    """
    observed=[]
    for row in observations if isinstance(observations,list) else []:
        if not isinstance(row,dict) or row.get("eligible") is True:
            continue
        decision=at(row.get("at"))
        quote=at(row.get("quote_at"))
        price=number(row.get("entry_price_sda"))
        features=row.get("features")
        peak=row.get("observed_peak_240m")
        gates=row.get("gates")
        if not (decision and quote and price is not None and price>0
                and isinstance(features,dict)
                and isinstance(gates,list) and bool(gates)
                and isinstance(peak,dict)
                and peak.get("source")=="genuine_swap_observed_peak_not_executable_fill"):
            continue
        age=(decision-quote).total_seconds()
        peak_at=at(peak.get("observed_at"))
        change=number(peak.get("price_return_pct"))
        if (not 0<=age<=300 or peak_at is None or change is None
                or not 0 < (peak_at-decision).total_seconds() <= 240*60):
            continue
        # Confirm at least a minimal pre-outcome immutable signal vector.
        # A 4h peak cannot recover features absent from original research.
        if any(number(features.get(k)) is None for k in (
                "score","quality","snapshot_impulse","m1h","m15",
                "trades_1h","volume_1h","flow_1h","flow_15m")):
            continue
        address=str(row.get("address") or "").lower()
        if not address:
            continue
        observed.append({"at":decision,"peak_at":peak_at,
                         "address":address,
                         "symbol":str(features.get("symbol") or "")[:20],
                         "change":change,"gates":sorted(set(map(str,gates)))})
    observed.sort(key=lambda r:(r["at"],r["address"]))
    latest={}
    distinct=[]
    for row in observed:
        last=latest.get(row["address"])
        if last is not None and (row["at"]-last).total_seconds()<OBSERVATION_COOLDOWN_SECONDS:
            continue
        latest[row["address"]]=row["at"]
        distinct.append(row)
    positive=[row for row in distinct if row["change"]>=10]
    blockers={}
    for row in positive:
        for gate in row["gates"]:
            blockers[gate]=blockers.get(gate,0)+1
    strongest=sorted(positive,key=lambda row:(-row["change"],row["at"]))[:5]
    return {
        "source":"actual_observed_swaps_original_frozen_rejected_decisions",
        "paper_only":True,"executable_peak_fills":False,
        "distinct_observed_4h_peaks":len(distinct),
        "observed_peak_ge_10_pct":len(positive),
        "observed_peak_ge_25_pct":sum(row["change"]>=25 for row in distinct),
        "observed_peak_ge_50_pct":sum(row["change"]>=50 for row in distinct),
        "blocking_gates_ge_10_pct":dict(sorted(
            blockers.items(),key=lambda x:(-x[1],x[0]))[:12]),
        "examples":[{"address":row["address"],"symbol":row["symbol"],
                     "decision_at":row["at"].isoformat(),
                     "peak_observed_at":row["peak_at"].isoformat(),
                     "observed_peak_return_pct":round(row["change"],2),
                     "gates":row["gates"]}
                    for row in strongest],
        "limitation":"Observed later swap highs are not executable fills, net SDA or a BUY validation target.",
    }



def early_horizon_research(observations):
    """Causal 5/15/60m missed-BUY outcome cohorts; NOT an executable fill.

    Require a verified original entry and the FIRST actually observed swap in
    the bounded horizon. Do not use a future peak, stale quote, legacy label
    without transaction source, or reuse one correlated token repeatedly.
    Results are diagnostic only; Champion selection remains 60m + forward.
    """
    windows={5:(5,10),15:(15,25),60:(60,75)}
    results={}
    rows=observations if isinstance(observations,list) else []
    for horizon,(earliest,latest) in windows.items():
        candidates=[]
        for row in rows:
            if not isinstance(row,dict) or row.get("eligible") is True:
                continue
            decision=at(row.get("at"))
            quote=at(row.get("quote_at"))
            entry=number(row.get("entry_price_sda"))
            frozen=row.get("features")
            gates=row.get("gates")
            out=(row.get("outcomes") or {}).get(f"{horizon}m")
            if (decision is None or quote is None or entry is None or entry<=0
                    or not 0 <= (decision-quote).total_seconds() <=300
                    or not isinstance(frozen,dict) or not isinstance(gates,list)
                    or not gates or not isinstance(out,dict)
                    or out.get("source")!="observed_later_market_transaction_not_executable_fill"):
                continue
            # Require a complete ORIGINAL vector, never reconstructed features.
            fields=("score","quality","snapshot_impulse","m1h","m15","m4h",
                    "trades_1h","buy_ratio","volume_1h","flow_1h","flow_15m")
            if frozen.get("phase") not in ("WATCH","IGNITION","CONFIRMATION","BREAKOUT"):
                continue
            if any(number(frozen.get(key)) is None for key in fields):
                continue
            observed=at(out.get("observed_at"))
            exit_price=number(out.get("price_sda"))
            if (observed is None or exit_price is None or exit_price<=0
                    or not earliest <= (observed-decision).total_seconds()/60 <=latest):
                continue
            address=str(row.get("address") or "").lower()
            if not address:
                continue
            candidates.append((decision,address,exit_price,entry,tuple(gates)))
        candidates.sort(key=lambda item:(item[0],item[1]))
        last={}
        distinct=[]
        for decision,address,exit_price,entry,gates in candidates:
            prev=last.get(address)
            if prev is not None and (decision-prev).total_seconds()<OBSERVATION_COOLDOWN_SECONDS:
                continue
            last[address]=decision
            distinct.append((decision,address,net_sda(entry,exit_price),gates))
        total=sum(pnl for _,_,pnl,_ in distinct)
        winning=[row for row in distinct if row[2]>0]
        winning_gates={}
        for _,_,_,gates in winning:
            for key in set(map(str,gates)):
                winning_gates[key]=winning_gates.get(key,0)+1
        results[f"{horizon}m"]={
            "strict_first_observed_swap_window_minutes":[earliest,latest],
            "valid_distinct_cases":len(distinct),
            "model_positive_after_costs":len(winning),
            "model_net_sda_total":round(total,4),
            "positive_gate_counts":dict(sorted(
                winning_gates.items(),key=lambda v:(-v[1],v[0]))[:10]),
            "research_only":True,"cannot_promote_champion":True,
        }
    return {
        "source":"original_frozen_features_verified_first_swap_only",
        "execution_parity":False,
        "paper_only":True,
        "horizons":results,
        "warning":"Historical observed quote outcomes include model friction, NOT actual entry/exit fills; no hindsight peaks.",
    }


def learn(observations):
    # Surface archive coverage independently of the strict causal training
    # subset. An empty valid sample is not the same as an empty archive.
    archive = observations if isinstance(observations,list) else []
    archived = sum(isinstance(row,dict) for row in archive)
    with_horizon = sum(
        isinstance((row.get("outcomes") or {}).get("60m"),dict)
        for row in archive if isinstance(row,dict)
        and isinstance(row.get("outcomes"),dict)
    )
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
    # Diagnostics only: compare frozen rejection reasons against later
    # 60m observed-quote outcomes. Counts overlap across the two lanes.
    reason_cohorts={}
    for item in all_rows:
        for lane,reason in item.get("rejection_reasons",{}).items():
            key=f"{lane}:{reason}"
            reason_cohorts.setdefault(key,[]).append(item)
    reason_outcomes={key:summary(rows) for key,rows in
                     sorted(reason_cohorts.items())}
    positives=[r for r in all_rows if r["pnl"]>=3]
    negatives=[r for r in all_rows if r["pnl"]<0]
    base={
        "mode":"OBSERVED_QUOTE_HYPOTHESIS_NO_EXECUTION_PARITY",
        "paper_only":True,"can_promote_champion":False,
        "sample_count":len(all_rows),
        "archived_candidate_snapshots":archived,
        "candidate_snapshots_with_60m_quote":with_horizon,
        "excluded_from_causal_learning":max(0,with_horizon-len(all_rows)),
        "input_quality":archive_quality_diagnostics(archive),
        "missed_pump_examples":len(positives),
        "positive_theoretical_60m_sda_model_not_fills":round(
            sum(r["pnl"] for r in positives),4),
        "negative_theoretical_60m_sda_model_not_fills":round(
            sum(r["pnl"] for r in negatives),4),
        "rejection_reason_60m_observed_cohorts":reason_outcomes,
        "gates_on_profitable_rejected_candidates":rejection_counts(positives),
        "gates_on_loss_making_rejected_candidates":rejection_counts(negatives),
        "potential_240m_peak_examples":sum(
            r["observed_peak_pct"] is not None and r["observed_peak_pct"]>=10
            for r in all_rows),
        "observed_4h_pump_research":observed_peak_diagnostics(archive),
        "early_horizon_research":early_horizon_research(archive),
        "objective":"net SDA at first 60m observed quote after 2.2% roundtrip model cost",
        "limitation":"no fill or intrabar peak assumed; 4h/token de-correlation; new rule requires untouched forward shadow",
        "train":summary(train),"validation_sample_count":len(valid),
        "status":"COLLECTING","candidate":None,
    }
    if (len(train)<MIN_TRAIN or len(valid)<MIN_VALIDATION
            or len({r["address"] for r in train})<MIN_UNIQUE_TRAIN
            or len({r["address"] for r in valid})<MIN_UNIQUE_VALIDATION):
        if archived and not all_rows:
            base["status"]="NO_VALID_MATURED_OUTCOMES"
        return base
    safe={"entry_lane":"missed_pump","entry_score":60,
          "quality":56,"impulse":8,"buy_ratio":1.5,"trades":6}
    def selected(data,policy):
        # The frozen candidate was captured before the observed future
        # price. Never turn BREAKOUT/NO into WATCH or stale age into 30s.
        return [r for r in data if safe_missed_entry(r["frozen_snapshot"],policy)]
    baseline_train=selected(train,safe)
    baseline_valid=selected(valid,safe)
    base["eligible_unmodified_train"]=summary(baseline_train)
    base["eligible_unmodified_validation"]=summary(baseline_valid)
    # Search only training observations. Evaluate single and two-signal
    # conjunctions; both are frozen BEFORE touching chronological validation.
    # Penalize complexity to avoid a lucky fit to a few historic pumps.
    from itertools import combinations
    alternatives=[(key,mode,bound)
                  for key,mode,thresholds in PARAMETERS for bound in thresholds]
    proposals=[]
    baseline_train_stats=summary(baseline_train)
    hypotheses=[(one,) for one in alternatives]
    hypotheses.extend((a,b) for a,b in combinations(alternatives,2)
                      if a[0]!=b[0])
    for rules in hypotheses:
        policy={**safe,"learned_rules":[
            {"feature":key,"mode":mode,"bound":bound}
            for key,mode,bound in rules]}
        t=selected(train,policy)
        if (len(t)<MIN_SELECTED_TRAIN
                or len({x["address"] for x in t})<MIN_UNIQUE_TRAIN):
            continue
        stats=summary(t)
        if (stats["net_sda_model"]<=0
                or stats["profit_factor_model"]<=1.12
                or stats["mean_sda_model"]<=baseline_train_stats["mean_sda_model"]+.30):
            continue
        complexity_penalty=.18*(len(rules)-1)
        objective=(stats["mean_sda_model"]-
                   .04*stats["max_drawdown_sda_model"]-complexity_penalty)
        proposals.append((objective,rules,policy,stats))
    if not proposals:
        return {**base,"status":"NO_PROFITABLE_TRAIN_RULE",
                "tested_rules":len(hypotheses)}
    proposals.sort(key=lambda x:(-x[0],len(x[1]),x[1]))
    _,rules,policy,tstat=proposals[0]
    # HOLDOUT is assessed ONCE for this single train-selected policy.
    v=selected(valid,policy)
    vstat=summary(v)
    passed=(vstat["samples"]>=MIN_SELECTED_VALIDATION
            and vstat["unique_tokens"]>=MIN_UNIQUE_VALIDATION
            and vstat["net_sda_model"]>0
            and vstat["profit_factor_model"]>1.10
            and vstat["mean_sda_model"]>
                summary(baseline_valid)["mean_sda_model"]+.15)
    candidate={"hypothesis":"missed_60m_net_sda:"+",".join(
        f"{key}:{mode}:{bound}" for key,mode,bound in rules),
               "policy":policy,"train":tstat,"validation":vstat,
               "selection":"one or two pre-registered features, train-only search, untouched chronological validation",
               "selection_bias_warning":"Outcome is a quoted 60m mark, NOT an executed trade. Verify on future Shadow portfolio.",
               "evidence_tier":"CHRONOLOGICAL_QUOTE_VALIDATION" if passed else "REJECTED_VALIDATION",
               "can_promote_paper":False,"can_trade_real":False}
    return {**base,"status":"VALIDATED_FOR_FORWARD_SHADOW" if passed
            else "TRAIN_RULE_FAILED_VALIDATION",
            "tested_rules":len(hypotheses),
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
        "archived":report["archived_candidate_snapshots"],
        "with_60m_quote":report["candidate_snapshots_with_60m_quote"],
        "input_quality":report["input_quality"],
        "observed_60m_positive":report["missed_pump_examples"],
        "proposed_rule":(report["candidate"] or {}).get("hypothesis")}))


if __name__=="__main__":
    main()
