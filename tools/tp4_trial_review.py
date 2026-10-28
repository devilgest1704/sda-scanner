"""Fixed forward milestones and journal checks; no rule tuning or promotion."""
from collections import defaultdict
from datetime import datetime, timezone
from tools.execution_quote_audit import timestamp
from tools.strategy_evidence import number

POLICY = "research_temporal_recovery_tp4"


def trade_errors(row):
    errors = []
    entry, exit_price, net = (number(row.get(k)) for k in (
        "entry_price_sda", "exit_price_sda", "pnl_sda"))
    opened, closed = timestamp(row.get("opened_at")), timestamp(row.get("closed_at"))
    if not opened or not closed or closed <= opened:
        errors.append("invalid_trade_time")
    if row.get("valid_for_learning") is not True:
        return errors  # Invalid observations stay in the risk totals.
    if number(row.get("notional_sda")) != 50:
        errors.append("unexpected_notional")
    if not entry or entry <= 0 or not exit_price or exit_price <= 0 or net is None:
        return errors+["invalid_price_or_pnl"]
    expected = 50*(exit_price*.989/(entry*1.011)-1)
    if abs(net-expected) > .00001:
        errors.append("cost_accounting_mismatch")
    e = row.get("tp4_entry_evidence") or {}
    if e.get("research_temporal_recovery_ready") is not True:
        errors.append("missing_frozen_temporal_confirmation")
    frozen, quoted = timestamp(e.get("at")), timestamp(e.get("quote_at"))
    if not frozen or not quoted or not 0 <= (frozen-quoted).total_seconds() <= 300:
        errors.append("invalid_entry_quote_time")
    observed = timestamp(row.get("exit_observed_at"))
    if not observed or not closed or not 0 <= (observed-closed).total_seconds() <= 300:
        errors.append("missing_fresh_exit_observation")
    if (number(row.get("max_quote_gap_minutes")) or 0) > 30:
        errors.append("holding_gap_in_positive_learning")
    reason = row.get("reason")
    move = exit_price/entry-1
    if reason == "TAKE" and move < .04-1e-10:
        errors.append("take_below_threshold")
    elif reason == "STOP" and move > -.025+1e-10:
        errors.append("stop_above_threshold")
    elif reason == "TRAIL":
        peak = number(row.get("peak_price_sda"))
        if not peak or peak <= 0 or exit_price/peak-1 > -.035+1e-10:
            errors.append("invalid_trailing_threshold")
        if opened and closed and (closed-opened).total_seconds() < 900:
            errors.append("trailing_before_15m")
    elif reason == "TIME" and opened and closed and (closed-opened).total_seconds() < 14400:
        errors.append("time_exit_before_4h")
    elif reason not in ("TAKE", "STOP", "TRAIL", "TIME"):
        errors.append("unsupported_valid_exit")
    return errors


def review(state, now=None, execution_audit=None):
    now = now or datetime.now(timezone.utc)
    group = state.get("strategies", {}).get(POLICY)
    if not isinstance(group, dict):
        return {"status": "TRIAL_NOT_STARTED", "can_promote": False}
    trades = group.get("trades") or []
    valid = [r for r in trades if isinstance(r, dict) and r.get("valid_for_learning") is True
             and number(r.get("pnl_sda")) is not None]
    ordered = sorted(valid, key=lambda r: str(r.get("closed_at") or ""))
    errors = [{"opened_at": r.get("opened_at"), "token": r.get("address"), "errors": found}
              for r in trades if isinstance(r, dict) and (found := trade_errors(r))]
    seen = set()
    for row in trades:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("address") or "").lower(), row.get("opened_at"))
        if key in seen:
            errors.append({"token": key[0], "opened_at": key[1], "errors": ["duplicate_entry_identity"]})
        seen.add(key)
        closed = timestamp(row.get("closed_at"))
        if closed and closed > now:
            errors.append({"token": key[0], "errors": ["future_close"]})
    token_net = defaultdict(float)
    for row in valid:
        token_net[str(row.get("address") or "").lower()] += float(row["pnl_sda"])
    values = [float(r["pnl_sda"]) for r in ordered]
    net, gains, loss = sum(values), sum(x for x in values if x > 0), -sum(x for x in values if x < 0)
    equity = peak = dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
    coverage = len(valid)/len(trades) if trades else 0
    positive_token_profit = sum(max(x, 0) for x in token_net.values())
    concentration = max(token_net.values(), default=0)/positive_token_profit if positive_token_profit else None
    count = len(valid)
    milestone = ("DECISION_REVIEW_50" if count >= 50 else "BREADTH_REVIEW_30" if count >= 30
                 else "PRELIMINARY_REVIEW_15" if count >= 15 else "COLLECTING" if trades
                 else "WAITING_FOR_FIRST_TRADE")
    blockers = []
    for failed, reason in (
        (count < 50, "fewer_than_50_valid_closes"),
        (len(token_net) < 6, "fewer_than_6_tokens"),
        (coverage < .9, "valid_quote_coverage_below_90pct"),
        (net <= 0, "nonpositive_net_after_model_costs"),
        (net-50*(len(trades)-len(valid)) <= 0, "nonpositive_conservative_closed_risk"),
        (loss > 0 and gains/loss < 1.15, "profit_factor_below_1_15"),
        (dd > 50, "drawdown_over_50_sda"),
        (concentration is None or concentration > .5, "profit_concentrated_in_one_token"),
        (bool(errors), "journal_integrity_errors"),
    ):
        if failed:
            blockers.append(reason)
    audit_at = timestamp((execution_audit or {}).get("updated_at"))
    return {
        "policy": POLICY, "source_at": state.get("updated_at"),
        "status": "JOURNAL_INTEGRITY_ERROR" if errors else milestone,
        "next_milestone_valid_closes": next((n for n in (15, 30, 50) if count < n), None),
        "closed": len(trades), "valid_closed": count, "unique_tokens": len(token_net),
        "open_positions": len(group.get("positions") or {}),
        "net_after_model_costs_sda": round(net, 6), "max_drawdown_sda": round(dd, 6),
        "conservative_closed_risk_sda": round(net-50*(len(trades)-len(valid)), 6),
        "quote_coverage_pct": round(100*coverage, 2),
        "profit_factor": gains/loss if loss else None,
        "largest_token_share_of_positive_token_profit": concentration,
        "journal_errors": errors[:15], "journal_error_count": len(errors),
        "research_quality_blockers": blockers,
        "research_review_ready": not blockers,
        "execution_audit_fresh": audit_at is not None and 0 <= (now-audit_at).total_seconds() <= 600,
        "execution_parity_verified": False, "can_promote": False,
        "note": "Milestones are reviews, not deployment approvals. Historical exits and quote simulations do not prove fills.",
    }
