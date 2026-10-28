"""Descriptive forward evidence, including unverified capital at risk.

Never selects a deployable strategy. Policies started at different times and
their cumulative returns are not a controlled head-to-head backtest.
"""
import math


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def evidence_report(state):
    rows = []
    for name, group in sorted(state.get("strategies", {}).items()):
        journal = group.get("trades") or []
        valid = [t for t in journal if isinstance(t, dict)
                 and t.get("valid_for_learning") is True
                 and number(t.get("pnl_sda")) is not None]
        valid_ids = {id(t) for t in valid}
        # An invalid observation is not evidence of a fill. Full stake loss
        # is a separate stress assumption, never called realized model P/L.
        invalid = [t for t in journal if id(t) not in valid_ids]
        risk = sum(max(50.0, number(t.get("notional_sda")) or 50.0)
                   if isinstance(t, dict) else 50.0 for t in invalid)
        net = sum(float(t["pnl_sda"]) for t in valid)
        open_count = len(group.get("positions") or {})
        values = [float(t["pnl_sda"]) for t in sorted(
            valid, key=lambda t: str(t.get("closed_at") or ""))]
        equity = peak = drawdown = 0.0
        for value in values:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        rows.append({
            "strategy": name, "closed": len(journal), "valid_closed": len(valid),
            "invalid_closed": len(invalid), "open_positions": open_count,
            "valid_quote_coverage_pct": round(100*len(valid)/len(journal), 2) if journal else 0,
            "observed_quote_net_sda": round(net, 6),
            "observed_quote_drawdown_sda": round(drawdown, 6),
            "unverified_full_stake_risk_sda": round(risk, 6),
            "conservative_closed_risk_sda": round(net-risk, 6),
            "open_stake_risk_sda": 50.0*open_count,
            "conservative_including_open_risk_sda": round(net-risk-50.0*open_count, 6),
            "can_promote": False,
        })
    def entries(name):
        group = state.get("strategies", {}).get(name, {})
        unique, duplicates = {}, set()
        for trade in group.get("trades") or []:
            if not isinstance(trade, dict):
                continue
            address, opened = trade.get("address"), trade.get("opened_at")
            if not address or not opened:
                continue
            key = (str(address).lower(), opened)
            if key in unique:
                duplicates.add(key)
            unique[key] = trade
        return {k: t for k, t in unique.items() if k not in duplicates
                and t.get("valid_for_learning") is True
                and number(t.get("pnl_sda")) is not None}
    parent = entries("research_temporal_recovery")
    tp4 = entries("research_temporal_recovery_tp4")
    paired = parent.keys() & tp4.keys()
    parent_net = sum(float(parent[k]["pnl_sda"]) for k in paired)
    tp4_net = sum(float(tp4[k]["pnl_sda"]) for k in paired)
    return {
        "source_at": state.get("updated_at"),
        "mode": "DESCRIPTIVE_FORWARD_RESEARCH_ONLY", "can_promote": False,
        "comparison_limit": "Different inception dates and entries; totals do not rank causal superiority",
        "risk_assumption": "Full stake loss for invalid closes and open positions, not observed realized P/L",
        "strategies": rows,
        "paired_tp4_exits": {
            "method": "same_address_and_opened_at_unique_valid_closes_only",
            "pairs": len(paired), "parent_net_sda": round(parent_net, 6),
            "tp4_net_sda": round(tp4_net, 6),
            "difference_sda": round(tp4_net-parent_net, 6),
            "unpaired_parent": len(parent)-len(paired),
            "unpaired_tp4": len(tp4)-len(paired),
            "limitation": "Unpaired and invalid outcomes remain in strategy risk totals; pairs alone cannot establish portfolio profitability",
        },
        "tp4_trial": {
            "policy": "research_temporal_recovery_tp4",
            "entry_reference": "research_temporal_recovery",
            "take_gross_pct": 4.0, "stop_gross_pct": -2.5,
            "trail_from_observed_peak_pct": 3.5, "max_hours": 4,
            "side_friction_pct": 1.1, "notional_sda": 50,
            "status": "UNPROVEN_FORWARD_ONLY", "can_promote": False,
            "selection_basis": "Temporal entry hypothesis plus historical earlier-exit TP4 hypothesis; joint profitability unproven",
        },
    }
