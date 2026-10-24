#!/usr/bin/env python3
"""Autonomous learning/promotion for paper V30, using FORWARD-OBSERVED shadow P/L.

Model selection reads TRAIN only; a chronological HOLDOUT is evaluated only
for the single selected challenger. No real-wallet orders. A new Champion is
eligible only after sufficient fee/slippage-adjusted paper observations; the
active paper runner is not restarted by the optimizer.
"""
from __future__ import annotations
import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from tools.forward_shadow_agent import POLICIES, when, finite
from tools.v30_exit_parity import audit_archive

STATE_PATH = Path("strategy_lab/forward_shadow_state.json")
CONFIG_PATH = Path("v30_agent_config.json")
STATS_PATH = Path("paper_stats.json")
POSITIONS_PATH = Path("positions.json")
REPORT_PATH = Path("strategy_lab/autonomous_agent_report.json")
MIN_TRAIN = 30
MIN_HOLDOUT = 15
MIN_UNIQUE = 6
MIN_HOLDOUT_PNL = 10.0
# Virtual exits currently use STOP -2.5%, TAKE +8%, TRAIL -3.5%, TIME 4h.
# The real V30 has adaptive stop, EARLY WEAKNESS and PUMP BREAKDOWN.
# Until independent parity testing covers entry AND exit models, forward
# results are research evidence ONLY and cannot authorize a paper promotion.
RUNTIME_EXECUTION_PARITY_VERIFIED = False
COOLDOWN_HOURS = 24


def summary(rows):
    rows = sorted(rows, key=lambda r: r["closed_at"])
    values = [float(r["pnl_sda"]) for r in rows]
    equity = peak = dd = 0.0
    for x in values:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    win = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    return {
        "closed": len(rows), "net_sda": round(sum(values), 4),
        "max_drawdown_sda": round(dd, 4),
        "win_rate_pct": round(100 * sum(v > 0 for v in values) / len(values), 2) if values else 0,
        "profit_factor": round(win / loss, 4) if loss else (99 if win else 0),
        "unique_tokens": len({r["address"] for r in rows}),
        "last_closed_at": rows[-1]["closed_at"] if rows else None,
    }


def forward_rows(strategy):
    rows = strategy.get("trades") or []
    return [r for r in rows if isinstance(r, dict) and r.get("valid_for_learning") is True
            and when(r.get("closed_at")) and when(r.get("opened_at"))
            and finite(r.get("pnl_sda")) is not None
            and isinstance(r.get("address"), str) and r["address"]]


def paired_trend_runner_exit_impact(baseline_rows, runner_rows):
    """Compare *only* equal address/opened_at entry pairs on valid close data.

    Identical opening entries isolate the effect of exit policy. Each full
    50-SDA entry is counted once; runner partial legs are never counted twice.
    Missing or unpaired observations are excluded, not credited as missed
    profit. Descriptive evidence only: never authorizes a promotion.
    """
    def distinct(rows):
        found = {}
        duplicates = set()
        for row in rows:
            key = (str(row["address"]).lower(), row["opened_at"])
            if key in found:
                duplicates.add(key)
            found[key] = row
        for key in duplicates:
            del found[key]
        return found
    base = distinct(baseline_rows)
    runner = distinct(runner_rows)
    pairs = sorted(set(base) & set(runner))
    total_base = sum(float(base[k]["pnl_sda"]) for k in pairs)
    total_runner = sum(float(runner[k]["pnl_sda"]) for k in pairs)
    difference = total_runner-total_base
    return {
        "method": "same_address_and_opened_at_valid_forward_entries_only",
        "paired_closed_trades": len(pairs),
        "unique_tokens": len({k[0] for k in pairs}),
        "baseline_paired_net_sda": round(total_base, 4),
        "runner_paired_net_sda": round(total_runner, 4),
        "runner_minus_baseline_sda": round(difference, 4),
        "runner_outperformed_pairs": sum(
            float(runner[k]["pnl_sda"]) > float(base[k]["pnl_sda"])
            for k in pairs),
        "unpaired_baseline_valid": len(base)-len(pairs),
        "unpaired_runner_valid": len(runner)-len(pairs),
        "paper_only": True,
        "can_promote_champion": False,
        "warning": "Descriptive paired exit comparison, not a randomized or proven-profitability test",
    }


def paper_realized_since(ledger, since):
    result = []
    for row in ledger.get("closed_trades", []):
        if not isinstance(row, dict) or row.get("status") != "CLOSED":
            continue
        opened = when(row.get("opened_at"))
        closed = when(row.get("closed_at"))
        profit = finite(row.get("closed_profit_sda"))
        if opened and closed and opened >= since and closed >= opened and profit is not None:
            result.append({"closed_at": closed.isoformat(), "pnl_sda": profit,
                           "address": str(row.get("address") or "")})
    return result


def report(state, config, real_ledger, now=None):
    now = now or datetime.now(timezone.utc)
    # Match v30_pump_hunter.PAPER_BUY_MODE without importing the live engine
    # (an optimizer must never initialize or mutate a trading worker).
    active_buy_mode = os.environ.get("SDA_PAPER_BUY_MODE", "temporal_recovery")
    if active_buy_mode not in ("baseline", "temporal_recovery"):
        raise ValueError("Unrecognized active PAPER BUY mode")
    if config.get("version") != "V30-AUTO-CONFIG-1":
        raise ValueError("Unrecognized Champion config")
    current = config.get("filters")
    if not isinstance(current, dict) or set(POLICIES["baseline"]) - set(current):
        raise ValueError("Invalid Champion filters")
    # Read-only observed PAPER exit evidence; NOT SHADOW execution parity.
    closed_for_parity = real_ledger.get("closed_trades", []) if isinstance(real_ledger, dict) else []
    if not isinstance(closed_for_parity, list):
        closed_for_parity = []
    exit_parity_evidence = audit_archive(closed_for_parity[-500:])
    model = {
        "version": "FORWARD-PAPER-AGENT-1",
        "updated_at": now.isoformat(),
        "mode": "PAPER_ONLY",
        "objective": "maximize accumulated net SDA subject to observed quote risk",
        "real_trading": False, "paper_config_automatic": active_buy_mode == "baseline",
        "active_paper_buy_mode": active_buy_mode,
        "optimizer_targets_active_buy": active_buy_mode == "baseline",
        "shadow_method": "forward observed quote fills, estimated friction, NOT real market execution",
        "v32_macd": {
            "mode": "RESEARCH_ONLY",
            "policies": ["research_macd_antipump", "research_macd_confirmation"],
            "may_promote_champion": False,
            "live_buy_rules_modified": False,
            "paper_buy_rules_modified": True,
            "paper_buy_gate": "1h completed observed MACD bearish+weakening veto; missing data reported UNAVAILABLE and does not invent a bullish signal",
            "shadow_fallback": "validated 1h_completed when sparse 5m/15m unavailable",
            "caveat": "PAPER-only MACD entry veto is now active; benefit in SDA remains unproven until new paired forward observations",
        },
        "v32_breakout": {
            "mode": "RESEARCH_ONLY",
            "policies": ["research_breakout_volume_trend"],
            "may_promote_champion": False,
            "live_buy_rules_modified": False,
            "trade_model": "Independent WATCH/IGNITION breakout volume trend shadow portfolio",
            "caveat": "Transaction-based signals and observed quotes; no execution parity or profitable forward holdout proved",
        },
        "v32_trend_runner": {
            "mode": "RESEARCH_ONLY",
            "policies": ["research_trend_runner"],
            "may_promote_champion": False,
            "live_buy_rules_modified": False,
            "entry_reference": "same V30 baseline Shadow entry filters",
            "exit_reference": "take 50% at first observed >=+8% move, trail remaining 3.5% from observed peak, hard stop and max 12h after partial",
            "cost_accounting": "one full 50 SDA entry; separate 50% exit legs charged round-trip fees only once each; aggregate ONE completed trade",
            "caveat": "No real fills, no historical hindsight, invalid quotes excluded, no verified profit or execution parity",
        },
        "v33_flow_continuation": {
            "mode": "RESEARCH_ONLY",
            "policy": "research_v33_flow_continuation",
            "source": "same-scan observed 15m and 1h swap activity",
            "evidence": "15m buy dominance, hourly turnover and bounded price momentum",
            "roundtrip_model_friction_pct": 2.2,
            "paper_champion_modified": False,
            "pool_liquidity_verified": False,
            "can_promote_champion": False,
            "diagnostic": ((state.get("last_diagnostic") or {}).get("v33_flow_research")
                           if isinstance(state.get("last_diagnostic"), dict) else None),
            "limitation": ("Swap turnover is not pool depth; forward quote exits are not "
                           "executable fills. No profit claim or automatic promotion."),
        },
        "v33_cost_aware_breakout": {
            "mode": "RESEARCH_ONLY",
            "policy": "research_v33_cost_aware_breakout",
            "source": "same-scan V32 observed breakout with stricter V33 filters",
            "cost_model": "1.5% estimated friction each side (including 0.4% stress), not verified fills",
            "exit_model": "observed +10% take, -2.5% stop, 4% trailing, max 3h",
            "break_even_rate_is_not_prediction": True,
            "paper_champion_modified": False,
            "pool_liquidity_verified": False,
            "can_promote_champion": False,
            "diagnostic": ((state.get("last_diagnostic") or {}).get("v33_cost_breakout_research")
                           if isinstance(state.get("last_diagnostic"), dict) else None),
            "limitation": "Unmatched exits and observed swap turnover cannot establish executable profit",
        },
        "runtime_parity": {
            "verified": RUNTIME_EXECUTION_PARITY_VERIFIED,
            "observed_paper_exit_audit": exit_parity_evidence,
            "shadow_exits": "fixed 2.5% stop, +8% take, -3.5% trailing, 4h timeout",
            "paper_exits": "V30 emergency stop, adaptive trailing, early weakness, pump breakdown and stale",
            "warning": "Unmatched exit models cannot validate promotion until independent parity tests pass",
        },
        "source_last_scan_at": state.get("updated_at"),
        "strategies": {},
        "champion": {"generation": config.get("generation", 0), "filters": current},
        "promotion_applied": False,
        "rollback_applied": False,
    }
    # Surface the missed-opportunity learner in the EXISTING AI optimizer
    # report. This is never used to override the live/PAPER BUY gates.
    missed_path=Path("strategy_lab/missed_pump_learning.json")
    try:
        missed=json.loads(missed_path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        missed={}
    if not isinstance(missed,dict):
        missed={}
    archive_at=when(missed.get("source_candidate_archive_updated_at"))
    archive_age=((now-archive_at).total_seconds()/60) if archive_at is not None else None
    quality=missed.get("input_quality") if isinstance(missed.get("input_quality"),dict) else {}
    model["missed_pump_learning"]={
        "status":missed.get("status","WAITING_FOR_CANDIDATE_OUTCOMES"),
        "updated_at":missed.get("updated_at"),
        "source_candidate_archive_at":missed.get("source_candidate_archive_updated_at"),
        "source_age_minutes":round(archive_age,1) if archive_age is not None else None,
        "source_fresh":archive_age is not None and 0<=archive_age<=30,
        "archived_candidates":missed.get("archived_candidate_snapshots",0),
        "with_60m_quote":missed.get("candidate_snapshots_with_60m_quote",0),
        "input_quality":quality,
        "observed_candidates":missed.get("sample_count",0),
        "missed_60m_positive_after_costs":missed.get("missed_pump_examples",0),
        "observed_4h_pump_research":missed.get("observed_4h_pump_research",{}),
        "early_horizon_research":missed.get("early_horizon_research",{}),
        "learned_rule":(missed.get("candidate") or {}).get("hypothesis"),
        "research_only":True,
        "paper_buy_rules_modified":False,
        "forward_execution_parity_verified":RUNTIME_EXECUTION_PARITY_VERIFIED,
        "reason":"Train/holdout selects a rule; new forward Shadow must prove net SDA; unmatched exits prohibit auto PAPER promotion.",
    }
    # Immutable forward signal-timing research is read-only and never affects
    # promotion, PAPER loss circuits, or Champion gate thresholds.
    timing=state.get("timing_research") if isinstance(state,dict) else None
    model["temporal_recovery_research"]={
        "source_at": timing.get("last_at") if isinstance(timing,dict) else None,
        "total_recovered": timing.get("total_recovered",0) if isinstance(timing,dict) else 0,
        "recent_examples": timing.get("events",[])[-3:] if isinstance(timing,dict)
                           and isinstance(timing.get("events"),list) else [],
        "shadow_policy": "research_temporal_recovery",
        "paper_only":True,"champion_modified":False,
        "condition":"strong stale signal followed by genuinely new fresh quote and current activity",
    }
    # Scanner checkpoints publish a small auditable NO-BUY diagnosis.
    # Its timestamp (not this optimizer's run time) is the evidence age.
    # Never pretend that a missing diagnostic means a healthy BUY pipeline.
    buy_audit_path = Path("v30_no_buy_diagnostic.json")
    try:
        buy_audit = json.loads(buy_audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        buy_audit = None
    if isinstance(buy_audit, dict) and buy_audit.get("mode") == "READ_ONLY_PAPER_NO_BUY_DIAGNOSTIC":
        guard = buy_audit.get("paper_loss_circuit") or {}
        funnel = buy_audit.get("funnel") or {}
        checked_at = when(buy_audit.get("checked_at"))
        source_age = (now - checked_at).total_seconds()/60 if checked_at else None
        model["scanner_no_buy"] = {
            "assessment": buy_audit.get("assessment"),
            "checked_at": buy_audit.get("checked_at"),
            "audit_fresh": source_age is not None and 0 <= source_age <= 30,
            "audit_age_minutes": round(source_age, 1) if source_age is not None else None,
            "risk_blocked": guard.get("blocked"),
            "loss_last_24h_sda": guard.get("window_pnl_sda"),
            "loss_window_closed": guard.get("window_closed"),
            "projected_recovery_at_no_new_closes": guard.get(
                "projected_recovery_at_no_new_closes"),
            "risk_only_buy_ready": funnel.get("would_buy_if_only_loss_circuit_cleared"),
            "fresh_tokens": funnel.get("fresh_tokens"),
            "champion_ready": funnel.get("champion_ready"),
            "paper_buy_rules_modified": False,
        }
    else:
        model["scanner_no_buy"] = None

    last = when(state.get("updated_at"))
    if last is None or not (0 <= (now-last).total_seconds() <= 1800):
        model["status"] = "WAITING_FOR_FRESH_SCANNER"
        model["reason"] = "Forward scanner observations older than 30 minutes"
        return model, config

    # Guardrail: automatically roll back *paper only* if actual forward paper
    # outcomes after activation are negative after a meaningful sample.
    promotion = config.get("promotion")
    promoted_at = when(promotion.get("at")) if isinstance(promotion, dict) else None
    if promoted_at and config.get("previous_filters"):
        actual = paper_realized_since(real_ledger, promoted_at)
        actual_stats = summary(actual)
        model["post_promotion_actual_paper"] = actual_stats
        if ((actual_stats["closed"] >= 8 and actual_stats["net_sda"] < -25)
                or (actual_stats["closed"] >= 15 and actual_stats["net_sda"] < 0)
                or (actual_stats["closed"] >= 8 and actual_stats["max_drawdown_sda"] > 50)):
            previous = config["previous_filters"]
            config = {
                "version": "V30-AUTO-CONFIG-1", "generation": int(config.get("generation", 0)) + 1,
                "updated_at": now.isoformat(), "source": "FORWARD_AGENT_AUTO_ROLLBACK_PAPER",
                "filters": previous, "previous_filters": None, "promotion": None,
            }
            model.update({"status": "ROLLED_BACK_PAPER", "reason": "Observed actual paper risk limit reached",
                          "rollback_applied": True,
                          "champion": {"generation": config["generation"], "filters": previous}})
            return model, config
        if actual_stats["closed"] < 20 or (now-promoted_at).total_seconds() < COOLDOWN_HOURS*3600:
            model["status"] = "MONITORING_PAPER_PROMOTION"
            model["reason"] = "Waiting for forward paper validation before another change"
            return model, config

    # Compare against the CURRENT paper Champion, not always the original
    # baseline. Otherwise a second promotion can be worse than the active
    # strategy while appearing better than an obsolete baseline.
    incumbent_name = next(
        (name for name, policy in POLICIES.items()
         if not name.startswith("research_")
         and all(abs(float(current[key]) - float(value)) <= 1e-8
                 for key, value in policy.items())),
        None,
    )
    if incumbent_name is None:
        model["status"] = "UNKNOWN_INCUMBENT_POLICY"
        model["reason"] = "Active paper filters have no matching forward shadow journal"
        return model, config
    model["incumbent_policy"] = (
        "research_temporal_recovery" if active_buy_mode == "temporal_recovery"
        else incumbent_name)
    model["shadow_comparison_reference"] = incumbent_name

    strategies = state.get("strategies", {})
    if not isinstance(strategies, dict):
        raise ValueError("Incomplete forward strategy journals")
    # An older running scanner may not yet have checkpointed the new research
    # arms. Champion journals remain mandatory; optional research starts empty.
    from tools.forward_shadow_agent import default_state
    for name in POLICIES:
        if name not in strategies and name.startswith("research_"):
            strategies[name] = default_state()["strategies"][name]
    if any(name not in strategies for name in POLICIES):
        raise ValueError("Incomplete forward Champion strategy journals")

    all_rows = {name: forward_rows(strategies[name]) for name in POLICIES}
    coverage = {}
    for name in POLICIES:
        all_records = strategies[name].get("trades") or []
        valid = len(all_rows[name])
        coverage[name] = round(100 * valid / len(all_records), 2) if all_records else 0.0
    model["v32_trend_runner"]["paired_exit_comparison"] = (
        paired_trend_runner_exit_impact(
            all_rows.get("baseline", []),
            all_rows.get("research_trend_runner", []),
        )
    )
    model["quote_coverage_pct"] = coverage
    # Explain why a strategy lacks usable forward evidence. Do not infer
    # missing prices and do not weaken the observed-quote promotion gate.
    model["learning_data_quality"] = {}
    for name, strategy in strategies.items():
        if name not in POLICIES:
            continue
        journal = strategy.get("trades") or []
        invalid = [r for r in journal if isinstance(r, dict)
                   and r.get("valid_for_learning") is not True]
        breakdown = {}
        for trade in invalid:
            reasons = trade.get("learning_exclusion_reasons") or []
            if not reasons:
                # Older checkpoints may predate explicit exclusion codes.
                reasons = (["stale_timeout_no_observed_exit_quote"]
                           if trade.get("reason") == "STALE_TIMEOUT"
                           else ["legacy_unknown_quote_quality"])
            for reason in set(reasons):
                breakdown[str(reason)] = breakdown.get(str(reason), 0) + 1
        model["learning_data_quality"][name] = {
            "total_closed": len(journal),
            "valid_closed": len(all_rows[name]),
            "excluded_closed": len(invalid),
            "excluded_by_reason": breakdown,
            "open_virtual_positions": len(strategy.get("positions") or {}),
        }
    if active_buy_mode == "temporal_recovery":
        # The V30 numeric configuration only drives the baseline lane.
        # Its policy optimizer cannot tune temporal entry/continuation rules.
        # Show genuine temporal forward outcomes, never promote a baseline
        # numeric challenger as though it improved the active PAPER BUY.
        model["active_paper_evidence"] = {
            "strategy": "research_temporal_recovery",
            "valid_forward_closes": len(all_rows["research_temporal_recovery"]),
            "observed_quote_model": summary(all_rows["research_temporal_recovery"]),
            "quote_coverage_pct": coverage["research_temporal_recovery"],
            "execution_parity_verified": False,
        }
        model["status"] = "ACTIVE_TEMPORAL_MODE_RESEARCH_ONLY"
        model["reason"] = (
            "PAPER uses temporal_recovery, but this optimizer only tunes baseline "
            "numeric filters. Temporal forward evidence is diagnostic and lacks "
            "execution parity; no unrelated filter promotion is permitted.")
        return model, config
    unique_times = sorted({r["closed_at"] for rows in all_rows.values() for r in rows})
    if len(unique_times) < MIN_TRAIN + MIN_HOLDOUT:
        model["status"] = "COLLECTING_FORWARD_TRADES"
        model["reason"] = "Not enough separately observed chronological outcomes"
        for name, rows in all_rows.items():
            model["strategies"][name] = {"all": summary(rows)}
        return model, config

    boundary = unique_times[int(len(unique_times)*0.7)]
    candidates = []
    incumbent_holdout = None
    for name, rows in all_rows.items():
        train = [r for r in rows if r["closed_at"] < boundary]
        holdout = [r for r in rows if r["closed_at"] >= boundary]
        train_stats = summary(train)
        model["strategies"][name] = {
            "all": summary(rows), "train": train_stats,
            "holdout_sample_count": len(holdout),
            # No holdout P/L evaluated before selecting a winner.
        }
        if name == incumbent_name:
            incumbent_holdout = holdout
        if (name != incumbent_name and not name.startswith("research_")
                and coverage[name] >= 90.0
                and train_stats["closed"] >= MIN_TRAIN
                and train_stats["unique_tokens"] >= MIN_UNIQUE
                and train_stats["net_sda"] > 0
                and train_stats["profit_factor"] > 1.05):
            objective = train_stats["net_sda"] - 0.35*train_stats["max_drawdown_sda"]
            candidates.append((objective, name))
    model["split"] = {"method": "chronological_70_30_train_holdout",
                      "first_holdout_at": boundary, "train_selection_only": True}
    if not candidates:
        model["status"] = "NO_PROFITABLE_TRAIN_CHALLENGER"
        model["reason"] = "No challenger had sufficient positive after-cost training returns"
        return model, config

    candidates.sort(reverse=True)
    name = candidates[0][1]
    selected = [r for r in all_rows[name] if r["closed_at"] >= boundary]
    challenger = summary(selected)
    incumbent = summary(incumbent_holdout)
    proposal = POLICIES[name]
    model["selected_challenger"] = {"name": name, "filters": proposal,
                                    "train_score": round(candidates[0][0], 4),
                                    "holdout": challenger, "baseline_holdout": incumbent}
    # Evaluate holdout only once for the selected policy. Other contenders'
    # holdout outcomes remain inaccessible to the search objective.
    score = (challenger["closed"] >= MIN_HOLDOUT
             and challenger["unique_tokens"] >= MIN_UNIQUE
             and challenger["net_sda"] >= MIN_HOLDOUT_PNL
             and challenger["net_sda"] >= incumbent["net_sda"] + 10
             and challenger["profit_factor"] >= 1.15
             and challenger["max_drawdown_sda"] <= 50
             and challenger["win_rate_pct"] >= 35)
    changed = any(abs(float(current[k])-float(proposal[k])) > 1e-8 for k in proposal)
    if not score or not changed:
        model["status"] = "HOLDOUT_REJECTED" if not score else "CHAMPION_ALREADY_SELECTED"
        model["reason"] = "Insufficient positive untouched forward holdout or no filter change"
        return model, config

    # A further paper promotion cannot reuse the same completed forward
    # holdout after the last promotion: require fresh trades since that time.
    if promoted_at:
        new = [r for r in all_rows[name] if when(r["opened_at"]) > promoted_at]
        if len(new) < MIN_TRAIN:
            model["status"] = "WAITING_FOR_NEW_EVIDENCE"
            model["reason"] = "Need 30 new challenger trades after previous promotion"
            return model, config
    if not RUNTIME_EXECUTION_PARITY_VERIFIED:
        model["status"] = "RUNTIME_PARITY_UNVERIFIED"
        model["reason"] = ("Challenger passed numerical tests, but shadow entry/exit "
                           "execution has not been proven identical to V30 paper. "
                           "Automatic paper promotion blocked; research continues.")
        return model, config

    next_config = {
        "version": "V30-AUTO-CONFIG-1",
        "generation": int(config.get("generation", 0)) + 1,
        "updated_at": now.isoformat(), "source": "FORWARD_AGENT_PAPER_PROMOTION",
        "filters": proposal.copy(), "previous_filters": current.copy(),
        "promotion": {"at": now.isoformat(), "name": name,
                      "train_score": model["selected_challenger"]["train_score"],
                      "holdout": challenger, "baseline_holdout": incumbent,
                      "evidence": "FORWARD_QUOTE_SIMULATION_NOT_REAL_FILLS"},
    }
    model.update({"status": "PROMOTED_PAPER", "promotion_applied": True,
                  "champion": {"generation": next_config["generation"], "filters": proposal},
                  "reason": "Positive forward shadow train/holdout with bounded drawdown"})
    return model, next_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(STATE_PATH))
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--positions", default=str(POSITIONS_PATH))
    ap.add_argument("--output", default=str(REPORT_PATH))
    args = ap.parse_args()
    src = Path(args.state)
    if not src.exists():
        shadow = {"updated_at": None, "strategies": {}}
    else:
        shadow = json.loads(src.read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ledger = json.loads(Path(args.positions).read_text(encoding="utf-8"))
    result, updated = report(shadow, config, ledger)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Paper config is updated only when evidence supports promotion or rollback.
    if result["promotion_applied"] or result["rollback_applied"]:
        p = Path(args.config)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(p)
    print("AUTONOMOUS PAPER AGENT: " + json.dumps({
        "status": result["status"], "generation": result["champion"]["generation"],
        "promotion_applied": result["promotion_applied"],
        "rollback_applied": result["rollback_applied"],
        "selected": (result.get("selected_challenger") or {}).get("name"),
    }))


if __name__ == "__main__":
    main()
