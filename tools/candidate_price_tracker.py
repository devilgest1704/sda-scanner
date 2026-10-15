#!/usr/bin/env python3
"""Paper research only: join prior candidate observations to later market snapshots.

Never infer missing prices; observations require strictly later timestamps.
This produces marked-to-market hypothetical price returns, NOT executable P/L.
"""
import argparse
import json
import math
from datetime import datetime,timezone
from pathlib import Path

HORIZONS=(15,60,240,1440)
SAMPLE_MINUTES=30  # one causal snapshot per token per interval
MAX_ENTRY_QUOTE_AGE_SEC=300
MODEL_FRICTION_PER_SIDE=.011
MODEL_NOTIONAL_SDA=50.0
FROZEN_FEATURES=("score","quality","snapshot_impulse","m1h","m15",
                 "m4h","trades_1h","buy_ratio","volume_1h","flow_1h","flow_15m")
def parse_time(s):
    if not isinstance(s,str):return None
    try:return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)
    except (ValueError,TypeError):return None
def positive(v):
    try:
        x=float(v)
        return x if math.isfinite(x) and x>0 else None
    except (ValueError,TypeError):return None
def original_snapshot_complete(row):
    """An immutable entry reference, never reconstructed from later quotes."""
    if not isinstance(row,dict):
        return False
    t=parse_time(row.get("at"))
    quote=parse_time(row.get("quote_at"))
    features=row.get("features")
    if (t is None or quote is None or
            not 0<=(t-quote).total_seconds()<=MAX_ENTRY_QUOTE_AGE_SEC
            or positive(row.get("entry_price_sda")) is None
            or not isinstance(features,dict) or
            not all(isinstance(features.get(k),(int,float)) and
                    not isinstance(features.get(k),bool) and
                    math.isfinite(features[k]) for k in FROZEN_FEATURES)):
        return False
    return isinstance(row.get("gates"),list) and bool(row["gates"])


def update(state,market,max_observations=5000):
    if not isinstance(state,dict) or not isinstance(market,dict):raise ValueError("state and market must be objects")
    now=parse_time(market.get("updated_at"))
    if now is None:
        # The scanner may emit a market snapshot without a top-level
        # timestamp. Use the newest token quote only when it is parseable;
        # never substitute wall-clock time for an unverified market quote.
        quote_times=[]
        for td in (market.get("tokens",{}) or {}).values():
            if not isinstance(td,dict):continue
            a=td.get("analysis",td)
            if not isinstance(a,dict):continue
            t=parse_time(a.get("last_transaction") or td.get("last_transaction"))
            if t is not None:quote_times.append(t)
        now=max(quote_times) if quote_times else None
    if now is None:raise ValueError("market snapshot has no valid quote timestamp")
    tokens=market.get("tokens",{})
    if not isinstance(tokens,dict):raise ValueError("tokens must be object")
    existing=state.get("observations",[])
    if not isinstance(existing,list):existing=[]
    by_key={}
    observed_slots=set()
    for row in existing:
        if isinstance(row,dict) and row.get("id"):
            by_key[str(row["id"])]=row
            when=parse_time(row.get("at"))
            # Legacy rows lacking ORIGINAL quote time/features cannot
            # monopolize a sampling slot. Rebuild only when we still have
            # the exact contemporaneous scanner funnel, never from later
            # market_analysis (which would be lookahead).
            if original_snapshot_complete(row) and when is not None and row.get("address"):
                observed_slots.add((str(row["address"]).lower(),
                                    int(when.timestamp())//(60*SAMPLE_MINUTES)))
    for funnel in state.get("funnels",[]):
        if not isinstance(funnel,dict):continue
        at=parse_time(funnel.get("at"))
        if at is None or at>now:continue
        for item in funnel.get("candidate_snapshots",[]):
            if not isinstance(item,dict):continue
            address=str(item.get("address") or "").lower()
            price=positive(item.get("price_sda"))
            if not address or price is None:continue
            stamp=item.get("at") or funnel.get("at")
            t=parse_time(stamp)
            if t is None or t>now:continue
            # Freeze a single REAL, fresh scanner decision per token/30min.
            # Previously 120 candidates each minute evicted 4h observations
            # from the 5000-row rolling archive BEFORE they matured.
            if item.get("phase") not in ("WATCH","IGNITION","CONFIRMATION","BREAKOUT"):
                continue
            quote_at=parse_time(item.get("quote_at"))
            age=(t-quote_at).total_seconds() if quote_at is not None else None
            if age is None or not 0<=age<=MAX_ENTRY_QUOTE_AGE_SEC:
                continue
            if positive(item.get("volume_1h")) is None:
                continue
            slot=(address,int(t.timestamp())//(60*SAMPLE_MINUTES))
            if slot in observed_slots:
                continue
            key=f"{stamp}|{address}"
            prior=by_key.get(key)
            if prior is not None and original_snapshot_complete(prior):
                continue
            # A legacy row with the identical address/decision timestamp
            # may be repaired from the ORIGINAL immutable funnel only if
            # its archived entry price agrees with the captured quote.
            if prior is not None:
                old_entry=positive(prior.get("entry_price_sda"))
                if old_entry is None or abs(old_entry/price-1)>1e-9:
                    continue
            if prior is None or not original_snapshot_complete(prior):
                features={k:item.get(k) for k in (
                    "symbol","phase","score","quality","impulse",
                    "delta_impulse","snapshot_impulse","data_age_sec",
                    "m1h","m15","m4h","trades_1h","buy_ratio",
                    "volume_1h","flow_1h","flow_15m",
                    "research_breakout_ready","research_sparse_volume_candidate")}
                by_key[key]={
                    "id":key,"at":stamp,"address":address,
                    "quote_at":quote_at.isoformat(),
                    "entry_price_sda":price,
                    "eligible":bool(item.get("eligible")),
                    "score":item.get("score"),"quality":item.get("quality"),
                    "impulse":item.get("impulse"),
                    "gates":list(item.get("gates") or []),
                    "features":features,
                    # A previously observed later quote remains valid
                    # ONLY when original entry price/timestamp agree.
                    "outcomes":(prior.get("outcomes") if isinstance(
                        prior,dict) and isinstance(prior.get("outcomes"),dict)
                        else {})}
                if isinstance(prior,dict) and isinstance(prior.get("observed_peak_240m"),dict):
                    by_key[key]["observed_peak_240m"]=prior["observed_peak_240m"]
                observed_slots.add(slot)
    for row in by_key.values():
        start=parse_time(row.get("at"))
        entry=positive(row.get("entry_price_sda"))
        if start is None or entry is None or now<=start:continue
        td=tokens.get(row.get("address"),{})
        if not isinstance(td,dict):continue
        a=td.get("analysis",td)
        if not isinstance(a,dict):continue
        current=positive(a.get("price_in_sda"))
        if current is None:continue
        # Reject token-level stale quotes: market.updated_at alone is insufficient.
        quote_time=parse_time(a.get("last_transaction") or td.get("last_transaction"))
        if quote_time is None or quote_time<=start or quote_time>now:continue
        elapsed=(quote_time-start).total_seconds()/60
        outcomes=row.setdefault("outcomes",{})
        # Observed maximum is descriptive ONLY; no imaginary peak execution.
        if elapsed<=240:
            peak=row.get("observed_peak_240m")
            pct=round((current/entry-1)*100,4)
            if not isinstance(peak,dict) or pct>peak.get("price_return_pct",float("-inf")):
                row["observed_peak_240m"]={
                    "observed_at":quote_time.isoformat(),
                    "price_return_pct":pct}
        for horizon in HORIZONS:
            label=str(horizon)+"m"
            # First observed quote at/after the target horizon; 60m
            # labels require the stricter 75m learner window. These are
            # observations, not executable prices or exact-horizon fills.
            # The 60m AI learner accepts a later observed quote only
            # within 60-75m. Do not label a 90-120m observation "60m":
            # that silently creates unusable training rows and can hide
            # the absence of timely scanner checkpoints.
            max_elapsed=75 if horizon==60 else horizon*2
            if label not in outcomes and horizon<=elapsed<=max_elapsed:
                outcomes[label]={"observed_at":quote_time.isoformat(),
                    "elapsed_minutes":round(elapsed,2),
                    "price_sda":current,
                    "price_return_pct":round((current/entry-1)*100,4),
                    "net_return_pct_model":round(100*((current*(1-MODEL_FRICTION_PER_SIDE))/
                        (entry*(1+MODEL_FRICTION_PER_SIDE))-1),4),
                    "net_sda_model":round(MODEL_NOTIONAL_SDA*(
                        (current*(1-MODEL_FRICTION_PER_SIDE))/
                        (entry*(1+MODEL_FRICTION_PER_SIDE))-1),5)}
    ordered=sorted(by_key.values(),key=lambda r:str(r.get("at","")))
    return {"schema_version":1,"updated_at":now.isoformat(),
            "method":"paper_observed_quotes_not_executable_backtest",
            "sample_minutes":SAMPLE_MINUTES,
            "model_friction_per_side":MODEL_FRICTION_PER_SIDE,
            "observations":ordered[-max_observations:],
            "promotion_eligible":False,"production_mutated":False}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--state",default="v30_execution_state.json")
    ap.add_argument("--market",default="market_analysis.json")
    ap.add_argument("--output",default="strategy_lab/candidate_outcomes.json")
    args=ap.parse_args()
    state=json.loads(Path(args.state).read_text(encoding="utf-8"))
    market=json.loads(Path(args.market).read_text(encoding="utf-8"))
    dest=Path(args.output)
    if dest.exists():
        previous=json.loads(dest.read_text(encoding="utf-8"))
        if isinstance(previous,dict):
            state=dict(state)
            state["observations"]=previous.get("observations",[])
    result=update(state,market)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("Candidate observations:",len(result["observations"]))
if __name__=="__main__":main()
