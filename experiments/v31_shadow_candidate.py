"""Read-only V31 shadow decision; no order execution."""
def evaluate(snapshot):
    age=snapshot.get("data_age_sec")
    trades=snapshot.get("trades_1h")
    reasons=[]
    if not isinstance(age,(int,float)) or age<0:
        reasons.append("AGE_MISSING")
    elif age>300:
        reasons.append("AGE_STALE")
    if not isinstance(trades,(int,float)):
        reasons.append("TRADES_MISSING")
    elif trades<12:
        reasons.append("LOW_ACTIVITY")
    return {"eligible":not reasons,"reasons":reasons}
