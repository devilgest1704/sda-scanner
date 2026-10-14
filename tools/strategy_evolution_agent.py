#!/usr/bin/env python3
"""Evidence-driven PAPER research agent: diagnose -> propose -> forward-test -> learn.

No order placement, source rewriting, threshold grid search or live trading.
Historical filtering is selection-biased and is NEVER promotion evidence.
The optional LLM reviews aggregated diagnoses; its output cannot introduce code,
override safety thresholds, or authorize trading. Without an API key the same
bounded causal-diagnostic loop runs in evidence-only mode.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("strategy_lab/evolution_state.json")
TRADES = Path("v30_optimizer_trades.json")
FORWARD = Path("strategy_lab/forward_shadow_state.json")
MAX_EXPERIMENTS = 7  # reserve a slot for missed-pump forward research
MAX_ACTIVE = 2
MIN_TRAIN_MATCHES = 12
MIN_VALID_MATCHES = 6
MIN_VALID_RATIO = 0.35
FORWARD_REVIEW_MIN = 12
BASELINE = {"entry_score": 68, "quality": 52, "impulse": 8, "buy_ratio": 1.1, "trades": 5}

# The agent changes only these explicit PAPER-SHADOW entry hypotheses.
# Candidate entries must still satisfy all common V31 safety gates.
FEATURES = (
    ("avoid_late_hourly_chase", "m1h_max", "m1h", "max", 2.0, 11.0,
     ("m1h", "m1", "change_1h")),
    ("avoid_late_fifteen_min_chase", "m15_max", "m15", "max", 1.5, 10.0,
     ("m15", "change_15m")),
    ("require_stronger_buy_pressure", "buy_ratio", "buy_ratio", "min", 1.1, 5.0,
     ("buy_ratio",)),
    ("require_stronger_quality", "quality", "quality", "min", 52.0, 88.0,
     ("quality", "pump_quality")),
    ("require_stronger_impulse", "impulse", "impulse", "min", 8.0, 20.0,
     ("impulse", "pump_change")),
    ("require_confirmed_trade_activity", "trades", "trades_1h", "min", 12.0, 50.0,
     ("trades_1h", "trades")),
    ("require_stronger_net_flow", "flow_1h_min", "flow_1h", "min", 150.0, 5000.0,
     ("flow_1h", "flow")),
)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError, OverflowError):
        return None


def load(path, default):
    if not path.exists():
        return default
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Invalid JSON object {path}")
    return obj


def feature(row, keys):
    metrics = row.get("entry_metrics")
    if not isinstance(metrics, dict):
        return None
    for key in keys:
        if key in metrics and (v := number(metrics[key])) is not None:
            return v
    return None


def normalize_rows(rows):
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        m = row.get("entry_metrics")
        if not isinstance(m, dict) or not (
                isinstance(m.get("v30"), dict) or m.get("pump_quality") is not None):
            continue
        p = number(row.get("closed_profit_sda"))
        t = str(row.get("closed_at") or "")
        if p is None or not t:
            continue
        result.append({"pnl": p, "at": t, "metrics": m})
    result.sort(key=lambda x: x["at"])
    # Multiple partial/truncated archive rows must not be invented as trades.
    return result


def evaluate(rows, key, mode, bound, keys):
    observed = [row for row in rows
                if (value := feature({"entry_metrics": row["metrics"]}, keys)) is not None
                and ((value <= bound) if mode == "max" else (value >= bound))]
    n = len(observed)
    vals = [x["pnl"] for x in observed]
    gross_win = sum(v for v in vals if v > 0)
    gross_loss = -sum(v for v in vals if v < 0)
    return {"closed": n, "net_sda": round(sum(vals), 5),
            "avg_sda": round(sum(vals)/n, 5) if n else 0,
            "win_rate_pct": round(sum(x > 0 for x in vals)*100/n, 2) if n else 0,
            "profit_factor": round(gross_win/gross_loss, 4) if gross_loss else (99 if gross_win else 0),
            "coverage_pct": round(100*n/len(rows), 2) if rows else 0}


def baseline_stats(rows):
    vals = [x["pnl"] for x in rows]
    return {"closed":len(rows), "net_sda":round(sum(vals), 5),
            "avg_sda":round(sum(vals)/len(rows), 5) if rows else 0}


def proposals_from_evidence(rows):
    """Choose loss-vs-win separations, not an arbitrary Cartesian parameter grid.

    The chronological validation segment is scored only AFTER hypotheses were
    inferred from training data. Test data are not used to pick hypotheses.
    """
    cut1 = int(len(rows)*.60)
    cut2 = int(len(rows)*.80)
    train, validation = rows[:cut1], rows[cut1:cut2]
    if len(train) < 45 or len(validation) < 15:
        return [], {"status":"INSUFFICIENT_TRADE_HISTORY",
                    "train":len(train), "validation":len(validation)}
    base_train, base_valid = baseline_stats(train), baseline_stats(validation)
    choices = []
    exploratory = []
    feature_notes = []
    for description, policy_key, name, mode, floor, ceiling, aliases in FEATURES:
        win = [feature({"entry_metrics":r["metrics"]}, aliases)
               for r in train if r["pnl"] > 0]
        lose = [feature({"entry_metrics":r["metrics"]}, aliases)
                for r in train if r["pnl"] <= 0]
        win = [v for v in win if v is not None]
        lose = [v for v in lose if v is not None]
        if len(win) < 6 or len(lose) < 15:
            continue
        win_median, loss_median = statistics.median(win), statistics.median(lose)
        feature_notes.append({"hypothesis":description, "winning_median":round(win_median,4),
                              "losing_median":round(loss_median,4),
                              "winner_observations":len(win),
                              "loser_observations":len(lose)})
        # A hypothesis needs a directional difference that makes sense.
        if (mode == "max" and win_median >= loss_median) or (
                mode == "min" and win_median <= loss_median):
            continue
        bound = round(max(floor, min(ceiling, (win_median+loss_median)/2)), 3)
        selected_train = evaluate(train, policy_key, mode, bound, aliases)
        selected_valid = evaluate(validation, policy_key, mode, bound, aliases)
        if (selected_train["closed"] < MIN_TRAIN_MATCHES
                or selected_valid["closed"] < MIN_VALID_MATCHES
                or selected_train["coverage_pct"] < 100*MIN_VALID_RATIO
                or selected_valid["coverage_pct"] < 100*MIN_VALID_RATIO):
            continue
        # Filtering out every trade is NOT alpha. Both chronological partitions
        # must show better expected net SDA per trade than the unfiltered rows.
        advantage_train = selected_train["avg_sda"]-base_train["avg_sda"]
        advantage_valid = selected_valid["avg_sda"]-base_valid["avg_sda"]
        # VALIDATED requires improvement on both chronological partitions.
        # In an unprofitable market, EXPLORATORY is allowed for actual future
        # observed-quote research only, not for config promotion. It is driven
        # by measured winner/loss separation, never by a parameter grid.
        validated = advantage_train > 0.03 and advantage_valid > 0.03
        contrast = abs(win_median-loss_median)/max(1,abs(loss_median))
        if not validated and (contrast < 0.12 or advantage_train < -0.15):
            continue
        policy = dict(BASELINE)
        if policy_key in BASELINE:
            policy[policy_key] = max(policy[policy_key], bound)
        else:
            policy[policy_key] = bound
        candidate = {
            "hypothesis": description, "signal": name, "direction": mode,
            "evidence_tier": "HISTORICAL_VALIDATION" if validated else "EXPLORATION_ONLY",
            "limit": bound, "policy": policy,
            "train": selected_train, "validation": selected_valid,
            "baseline_train":base_train, "baseline_validation":base_valid,
            "train_avg_improvement_sda":round(advantage_train,5),
            "validation_avg_improvement_sda":round(advantage_valid,5),
            "rationale": f"Winning entry {name} median {win_median:.3f}; losing median {loss_median:.3f}. "
                         f"Test the {mode} {bound:g} guard on NEW future candidates.",
            "warning":"Historical survivors are selection-biased; improvement is NOT a causal or deployable return.",
        }
        (choices if validated else exploratory).append(candidate)
    ranking=lambda x:(x["validation_avg_improvement_sda"],
                      x["train_avg_improvement_sda"])
    qualified=sorted(choices,key=ranking,reverse=True)
    # The agent continues FORWARD research even when historical data cannot
    # justify a champion; exploratory hypotheses are always labelled clearly.
    selected=qualified if qualified else sorted(exploratory,key=lambda x:(
        x["train_avg_improvement_sda"],x["validation_avg_improvement_sda"]),reverse=True)[:2]
    return selected, {"status":"DIAGNOSED","train":len(train),"validation":len(validation),
                     "tested_hypothesis_families":len(FEATURES),
                     "validated_hypotheses":len(qualified),
                     "exploratory_hypotheses":len(exploratory),
                     "feature_diagnostics":feature_notes,
                     "baseline_train":base_train,"baseline_validation":base_valid}


def limited_llm_review(candidates, dataset):
    """Optional LLM hypothesis critique over aggregate data; no raw trade list.

    Models cannot submit code/parameters. Only select a pre-evaluated hypothesis
    or abstain. No API call if STRATEGY_LLM_API_KEY is not configured.
    """
    token = os.environ.get("STRATEGY_LLM_API_KEY", "").strip()
    if not token or not candidates:
        return {"status":"NOT_CONFIGURED" if not token else "NO_CANDIDATES"}
    model = os.environ.get("STRATEGY_LLM_MODEL", "gpt-4.1-mini")
    payload = {
        "model":model,
        "temperature":0.15,"max_tokens":300,
        "messages":[
            {"role":"system","content":
             "You review PAPER crypto trading hypotheses. Historical filtered trades are selection-biased. "
             "Choose a single hypothesis ID ONLY if validation supports a worthwhile FORWARD-ONLY experiment; "
             "otherwise abstain. Reply JSON only: {\"choice\":0, \"critique\":\"...\", \"risk\":\"...\"}. "
             "choice must be -1 or a zero-based index. You cannot alter thresholds or approve real trades."},
            {"role":"user","content":json.dumps({
                "data":dataset,"candidates":[{"id":i,"hypothesis":p["hypothesis"],
                "train":p["train"],"validation":p["validation"],
                "gain":p["validation_avg_improvement_sda"]} for i,p in enumerate(candidates[:4])]
            },ensure_ascii=False)}
        ],
    }
    try:
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=20) as response:
            raw=json.loads(response.read(35000).decode("utf-8"))
        reply=raw["choices"][0]["message"]["content"].strip()
        if reply.startswith("```"):
            reply=reply.strip("`").removeprefix("json").strip()
        decision=json.loads(reply)
        selected=decision.get("choice")
        if isinstance(selected,bool) or not isinstance(selected,int) or not (-1<=selected<min(4,len(candidates))):
            raise ValueError("LLM produced an invalid candidate ID")
        return {"status":"REVIEWED","model":model,"choice":selected,
                "critique":str(decision.get("critique",""))[:240],
                "risk":str(decision.get("risk",""))[:240]}
    except Exception as exc:
        # Secrets and potentially sensitive response text must never be logged.
        return {"status":"MODEL_UNAVAILABLE","reason":type(exc).__name__}


def initial_state():
    return {"schema":1,"paper_only":True,"mode":"EVIDENCE_DRIVEN_FORWARD_RESEARCH",
            "updated_at":None,"llm":{"status":"NOT_CONFIGURED"},
            "dataset_rows":0,"diagnosis":{},"experiments":[],"archive":[],"events":[]}


def experiment_policies(data):
    """Read-only, bounded, immutable research policies for the forward collector."""
    if not isinstance(data,dict) or data.get("schema") != 1 or data.get("paper_only") is not True:
        return {}
    result={}
    for e in data.get("experiments",[]):
        if not isinstance(e,dict) or e.get("status") not in ("FORWARD_TEST","RETIRED"):
            continue
        name=e.get("id")
        p=e.get("policy")
        if (not isinstance(name,str) or not name.startswith(("research_evo_","research_miss_"))
                or not isinstance(p,dict) or any(number(p.get(k)) is None for k in BASELINE)):
            continue
        if name.startswith("research_miss_") and p.get("entry_lane")!="missed_pump":
            continue
        if len(result) >= MAX_EXPERIMENTS:
            break
        result[name]=p
    return result


def evolve(state, trades, shadow, now=None, llm_reviewer=limited_llm_review, missed_report=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(state,dict) or state.get("schema") != 1 or state.get("paper_only") is not True:
        raise ValueError("Invalid evolution state; refusing to reset research memory")
    rows=normalize_rows(trades.get("trades",[]) if isinstance(trades,dict) else [])
    choices,diagnosis=proposals_from_evidence(rows)
    state["updated_at"]=now.isoformat()
    state["dataset_rows"]=len(rows)
    state["diagnosis"]={**diagnosis,"proposals_qualified":len(choices),
                       "leading_hypotheses": [
                           {"hypothesis":x["hypothesis"],"limit":x["limit"],
                            "evidence_tier":x["evidence_tier"],
                            "validation_avg_improvement_sda":x["validation_avg_improvement_sda"]}
                           for x in choices[:3]]}
    states=shadow.get("strategies",{}) if isinstance(shadow,dict) else {}
    for e in state["experiments"]:
        if e.get("status") not in ("FORWARD_TEST","RETIRED"):
            continue
        journal=states.get(e.get("id"),{}) if isinstance(states,dict) else {}
        valid=[v for v in journal.get("trades",[]) if isinstance(v,dict)
               and v.get("valid_for_learning") is True
               and number(v.get("pnl_sda")) is not None]
        e["forward_valid_closes"]=len(valid)
        e["forward_net_sda"]=round(sum(number(v["pnl_sda"]) for v in valid),5)
        e["forward_avg_sda"]=round(e["forward_net_sda"]/len(valid),5) if valid else None
        if e["status"]=="FORWARD_TEST" and len(valid)>=FORWARD_REVIEW_MIN:
            if e["forward_net_sda"] <= 0:
                e["status"]="RETIRED"
                e["retired_at"]=now.isoformat()
                e["conclusion"]="FAILED_FORWARD_PROFITABILITY"
            else:
                # A positive shadow experiment is NOT runtime parity proof.
                e["conclusion"]="POSITIVE_FORWARD_SHADOW_NEEDS_RUNTIME_PARITY"
    # A RETIRED arm finishes any open virtual risk before leaving the active
    # experiment registry. Its immutable result lives in an append-only
    # bounded archive; this allows lifelong learning, not a 6-experiment stop.
    state.setdefault("archive",[])
    keep=[]
    for e in state["experiments"]:
        journal=states.get(e.get("id"),{}) if isinstance(states,dict) else {}
        if (e.get("status")=="RETIRED" and e.get("retired_at") != now.isoformat()
                and not journal.get("positions")):
            state["archive"].append({
                "id":e["id"],"policy":e.get("policy"),
                "hypothesis":e.get("hypothesis"),"conclusion":e.get("conclusion"),
                "forward_valid_closes":e.get("forward_valid_closes",0),
                "forward_net_sda":e.get("forward_net_sda",0),
                "retired_at":e.get("retired_at"),
                "dataset_rows_at_retirement":len(rows),
                "missed_samples_at_retirement":e.get(
                    "missed_sample_count_at_proposal",0),
            })
        else:
            keep.append(e)
    state["archive"]=state["archive"][-80:]
    state["experiments"]=keep
    active=sum(e["status"]=="FORWARD_TEST" for e in state["experiments"])
    if active < MAX_ACTIVE and len(state["experiments"]) < MAX_EXPERIMENTS and choices:
        previous={json.dumps(e.get("policy"),sort_keys=True) for e in state["experiments"]}
        # Do not keep re-testing an identical failed experiment unless 35
        # more REAL closed PAPER outcomes provide a new analysis cohort.
        previous.update(json.dumps(e.get("policy"),sort_keys=True)
                        for e in state["archive"]
                        if len(rows)-e.get("dataset_rows_at_retirement",len(rows)) < 35)
        novel=[x for x in choices if json.dumps(x["policy"],sort_keys=True) not in previous]
        if novel:
            review=llm_reviewer(novel,diagnosis)
            state["llm"]=review
            if review.get("status")=="REVIEWED" and review.get("choice")==-1:
                novel=[]
            elif review.get("status")=="REVIEWED":
                index=review["choice"]
                novel=[novel[index]]
            for x in novel[:MAX_ACTIVE-active]:
                policy=x["policy"]
                # Include the new evidence cohort: a previously rejected
                # policy may be retested ONLY after enough independent closes.
                signature=json.dumps(policy,sort_keys=True)+":"+str(len(rows)//35)
                ident="research_evo_"+hashlib.sha256(signature.encode()).hexdigest()[:10]
                state["experiments"].append({
                    "id":ident,"status":"FORWARD_TEST","proposed_at":now.isoformat(),
                    "hypothesis":x["hypothesis"],"policy":policy,
                    "train":x["train"],"validation":x["validation"],
                    "evidence_tier":x["evidence_tier"],
                    "rationale":x["rationale"],"selection_bias_warning":x["warning"],
                    "forward_valid_closes":0,"forward_net_sda":0,
                    "conclusion":"AWAITING_NEW_FORWARD_OBSERVED_QUOTES",
                    "can_promote_paper":False,"can_trade_real":False,
                })
                state["events"].append({"at":now.isoformat(),"event":"CREATED_FORWARD_EXPERIMENT",
                                        "id":ident,"hypothesis":x["hypothesis"]})
    # Learn from previously REJECTED candidates and later observed quotes.
    # The result changes ONLY new forward Shadow BUY hypotheses.
    if isinstance(missed_report,dict):
        diagnosis=state.setdefault("diagnosis",{})
        diagnosis["missed_pump"]={
            "status":missed_report.get("status"),
            "observed_candidates":missed_report.get("sample_count",0),
            "missed_positive_60m":missed_report.get("missed_pump_examples",0),
            "learned_rule":(missed_report.get("candidate") or {}).get("hypothesis"),
            "can_promote_paper":False}
        chosen=missed_report.get("candidate")
        pending=[e for e in state["experiments"]
                 if e.get("status")=="FORWARD_TEST"
                 and str(e.get("id","")).startswith("research_miss_")]
        if (missed_report.get("status")=="VALIDATED_FOR_FORWARD_SHADOW"
                and isinstance(chosen,dict) and isinstance(chosen.get("policy"),dict)
                and not pending and len(state["experiments"])<MAX_EXPERIMENTS):
            policy=chosen["policy"]
            if (policy.get("entry_lane")=="missed_pump"
                    and all(number(policy.get(k)) is not None for k in BASELINE)):
                signature=json.dumps(policy,sort_keys=True)
                previous={json.dumps(e.get("policy"),sort_keys=True)
                          for e in state["experiments"]}
                previous.update(
                    json.dumps(e.get("policy"),sort_keys=True)
                    for e in state.get("archive",[])
                    if (missed_report.get("sample_count",0) -
                        e.get("missed_samples_at_retirement",
                              missed_report.get("sample_count",0))) < 35
                )
                if signature not in previous:
                    ident="research_miss_"+hashlib.sha256(signature.encode()).hexdigest()[:10]
                    state["experiments"].append({
                        "id":ident,"status":"FORWARD_TEST",
                        "proposed_at":now.isoformat(),
                        "hypothesis":chosen["hypothesis"],
                        "policy":dict(policy),
                        "train":chosen.get("train"),
                        "validation":chosen.get("validation"),
                        "evidence_tier":chosen["evidence_tier"],
                        "rationale":"Learned from rejected BUY decisions and later observed 60m SDA returns.",
                        "selection_bias_warning":chosen["selection_bias_warning"],
                        "forward_valid_closes":0,"forward_net_sda":0,
                        "missed_sample_count_at_proposal":missed_report["sample_count"],
                        "conclusion":"AWAITING_NEW_FORWARD_OBSERVED_QUOTES",
                        "can_promote_paper":False,"can_trade_real":False,
                    })
                    state["events"].append({
                        "at":now.isoformat(),
                        "event":"MISSED_PUMP_AUTO_SHADOW_FILTER_CREATED",
                        "id":ident,"hypothesis":chosen["hypothesis"]})
    state["events"]=state["events"][-50:]
    state["control"]={"real_trading":False,"paper_champion_unchanged":True,
                      "source_rewriting":False,"runtime_parity_verified":False,
                      "research_only":True,"open_research_arms":sum(
                          x["status"]=="FORWARD_TEST" for x in state["experiments"])}
    return state


def main():
    trades=load(TRADES,{"trades":[]})
    shadow=load(FORWARD,{"strategies":{}})
    state=load(STATE,initial_state())
    # Each scanner-hour provides fresh, causally tracked candidate outcomes.
    from tools.missed_pump_learning import learn
    opportunity_file=Path("strategy_lab/candidate_outcomes.json")
    missed_source=load(opportunity_file,{"observations":[]})
    if not isinstance(missed_source,dict) or not isinstance(
            missed_source.get("observations",[]),list):
        raise ValueError("Invalid candidate outcome research archive")
    missed_report=learn(missed_source.get("observations",[]))
    # Preserve original scanner archive timestamp. The agent's own current
    # timestamp is NOT proof that market observations are fresh.
    missed_report["source_candidate_archive_updated_at"]=missed_source.get("updated_at")
    missed_report["source_candidate_archive_method"]=missed_source.get("method")
    missed_report["updated_at"]=datetime.now(timezone.utc).isoformat()
    report_path=Path("strategy_lab/missed_pump_learning.json")
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_tmp=report_path.with_suffix(".tmp")
    report_tmp.write_text(json.dumps(missed_report,indent=2,ensure_ascii=False)+"\n",
                          encoding="utf-8")
    report_tmp.replace(report_path)
    result=evolve(state,trades,shadow,missed_report=missed_report)
    STATE.parent.mkdir(parents=True,exist_ok=True)
    tmp=STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    tmp.replace(STATE)
    print(json.dumps({"status":result["diagnosis"]["status"],
        "dataset":result["dataset_rows"],"llm":result["llm"]["status"],
        "experiments":[{"id":x["id"],"status":x["status"],
                        "forward_valid_closes":x["forward_valid_closes"],
                        "forward_net_sda":x["forward_net_sda"]} for x in result["experiments"]],
        "note":"PAPER shadow research ONLY, no Champion changes"}))


if __name__=="__main__":
    main()
