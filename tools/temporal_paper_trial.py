"""Isolated, forward-only PAPER trial of the temporal-recovery hypothesis.

Source entries and exits are produced by the existing forward Shadow agent.
This separate journal admits only NEW signals observed after activation,
with the live PAPER loss circuit and its own rolling loss budget applied.
Shadow quote-model results are NOT executable fills or the Champion ledger.
No real orders, no mutation of positions.json or v30_agent_config.json.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from tools.paper_loss_circuit import evaluate_closed, paper_entry_risk_gate

SOURCE = Path("strategy_lab/forward_shadow_state.json")
LEDGER = Path("strategy_lab/temporal_paper_trial.json")
REPORT = Path("strategy_lab/temporal_paper_trial_report.json")
STRATEGY = "research_temporal_recovery"
POSITION_SDA = 50.0
MAX_OPEN = 4


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def trade_key(address, opened_at):
    return str(address).lower() + "|" + str(opened_at)


def fresh_state(at):
    return {
        "schema": 1,
        "paper_only": True,
        "mode": "ISOLATED_TEMPORAL_RECOVERY_PAPER_TRIAL",
        "source": STRATEGY,
        "execution": "forward_observed_quote_model_not_executable_fills",
        "activated_at": at.isoformat(),
        "last_processed_at": None,
        "positions": {},
        "closed_trades": [],
        "rejected_entries": {"main_loss_circuit": 0, "trial_loss_circuit": 0,
                             "invalid_quote": 0, "capacity": 0},
    }


def validate_ledger(data):
    if (not isinstance(data, dict) or data.get("schema") != 1
            or data.get("paper_only") is not True or data.get("source") != STRATEGY
            or timestamp(data.get("activated_at")) is None
            or not isinstance(data.get("positions"), dict)
            or not isinstance(data.get("closed_trades"), list)
            or not isinstance(data.get("rejected_entries"), dict)):
        raise ValueError("Invalid temporal PAPER ledger: refusing reset")
    for key, position in data["positions"].items():
        if (not isinstance(position, dict) or position.get("id") != key
                or finite(position.get("entry_price_sda")) is None
                or timestamp(position.get("opened_at")) is None):
            raise ValueError("Corrupt temporal PAPER open position")
    for closed in data["closed_trades"]:
        if (not isinstance(closed, dict) or closed.get("status") != "CLOSED"
                or finite(closed.get("closed_profit_sda")) is None
                or timestamp(closed.get("closed_at")) is None):
            raise ValueError("Corrupt temporal PAPER closed trade")
    return data


def prepare_source(source):
    if (not isinstance(source, dict) or source.get("paper_only") is not True
            or source.get("method") != "forward_observed_quote_shadow_simulation"
            or timestamp(source.get("last_snapshot_at")) is None):
        raise ValueError("Forward quote source missing or invalid")
    group = (source.get("strategies") or {}).get(STRATEGY)
    if (not isinstance(group, dict) or not isinstance(group.get("positions"), dict)
            or not isinstance(group.get("trades"), list)):
        raise ValueError("Temporal recovery source portfolio missing")
    return group


def step(ledger, source, at, main_risk):
    """Settle previously admitted paper positions, then admit only NEW signals.

    A newly activated trial NEVER imports past Shadow P/L or open positions.
    Main and experiment loss circuits are checked only for NEW entries;
    existing exits continue even while blocked.
    """
    validate_ledger(ledger)
    group = prepare_source(source)
    source_at = timestamp(source["last_snapshot_at"])
    activated = timestamp(ledger["activated_at"])
    previous = timestamp(ledger.get("last_processed_at"))
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("Missing scan timestamp")
    at = at.astimezone(timezone.utc)
    if source_at > at or (at - source_at).total_seconds() > 180:
        raise ValueError("Stale/future temporal source; no phantom fills")
    if previous and source_at <= previous:
        return {"status": "already_processed", "opened": 0, "closed": 0}
    if not isinstance(main_risk, dict) or main_risk.get("blocked") is not False:
        main_blocked = True  # fail closed for unknown live risk
    else:
        main_blocked = False

    # Source trade records are keyed by token+original opening timestamp.
    # Keep existing admitted positions until a corresponding forward exit exists.
    completed = {}
    for tr in group["trades"]:
        if not isinstance(tr, dict):
            continue
        close_at = timestamp(tr.get("closed_at"))
        if close_at is None or close_at > at:
            continue
        key = trade_key(tr.get("address"), tr.get("opened_at"))
        completed[key] = tr

    closed = 0
    for key, position in list(ledger["positions"].items()):
        row = completed.get(key)
        if row is None:
            continue
        net = finite(row.get("pnl_sda"))
        if net is None:
            raise ValueError("Source closed trade has invalid P/L; preserving ledger")
        if net < -POSITION_SDA - 0.01:
            raise ValueError("Source loss exceeds paper stake; preserving ledger")
        # Ineligible stale/large-quote-gap source closes cannot be credited
        # as a profit. Conservatively record loss of the simulated stake.
        valid = row.get("valid_for_learning") is True
        accounted = round(net if valid else min(net, -POSITION_SDA), 6)
        ledger["closed_trades"].append({
            "id": key, "address": position["address"], "status": "CLOSED",
            "opened_at": position["opened_at"], "closed_at": row["closed_at"],
            "investment_sda": POSITION_SDA,
            "closed_profit_sda": accounted,
            "source_model_pnl_sda": round(net, 6),
            "valid_observed_quote": valid,
            "exit_reason": str(row.get("reason") or "UNKNOWN"),
            "execution": ledger["execution"],
        })
        del ledger["positions"][key]
        closed += 1

    own_risk = evaluate_closed(ledger["closed_trades"], at)
    opened = 0
    # A new paper trial can only follow a Shadow signal opened in THIS scan.
    # No research history is ever retroactively claimed as a PAPER trade.
    for address, position in group["positions"].items():
        if not isinstance(position, dict):
            continue
        opening = timestamp(position.get("opened_at"))
        key = trade_key(address, position.get("opened_at"))
        if (opening is None or opening != source_at or source_at < activated
                or key in ledger["positions"]
                or any(x["id"] == key for x in ledger["closed_trades"])):
            continue
        if main_blocked:
            ledger["rejected_entries"]["main_loss_circuit"] += 1
            continue
        if own_risk["blocked"]:
            ledger["rejected_entries"]["trial_loss_circuit"] += 1
            continue
        if len(ledger["positions"]) >= MAX_OPEN:
            ledger["rejected_entries"]["capacity"] += 1
            continue
        price = finite(position.get("entry_price_sda"))
        quote = timestamp(position.get("last_quote_at"))
        if (price is None or price <= 0 or quote is None or quote > opening
                or (opening - quote).total_seconds() > 300):
            ledger["rejected_entries"]["invalid_quote"] += 1
            continue
        ledger["positions"][key] = {
            "id": key, "address": str(address).lower(),
            "opened_at": opening.isoformat(),
            "entry_price_sda": price,
            "investment_sda": POSITION_SDA,
            "entry_quote_at": quote.isoformat(),
            "status": "OPEN",
        }
        opened += 1
        break  # at most one experimental entry per scan

    ledger["last_processed_at"] = source_at.isoformat()
    return {"status": "ok", "opened": opened, "closed": closed,
            "main_risk": str(main_risk.get("status", "UNKNOWN")),
            "trial_risk": own_risk["status"]}


def summary(ledger):
    closed = ledger["closed_trades"]
    # Performance must exclude unverifiable exits: they are NOT observed
    # trading losses or wins. Keep their full-stake conservative assumption
    # in the ledger for the independent loss circuit and safety monitoring.
    valid_rows = [x for x in closed if x.get("valid_observed_quote") is True]
    invalid_rows = [x for x in closed if x.get("valid_observed_quote") is not True]
    win = sum(x["closed_profit_sda"] > 0 for x in valid_rows)
    observed_pnl = round(sum(x["closed_profit_sda"] for x in valid_rows), 6)
    conservative_pnl = round(sum(x["closed_profit_sda"] for x in closed), 6)
    invalid_risk_pnl = round(sum(x["closed_profit_sda"] for x in invalid_rows), 6)
    valid = len(valid_rows)
    return {
        "mode": ledger["mode"],
        "source_strategy": STRATEGY,
        "updated_at": ledger["last_processed_at"],
        "activated_at": ledger["activated_at"],
        "paper_only": True, "real_trading": False,
        "separate_from_champion_accounting": True,
        "historical_shadow_profit_imported": False,
        "source_execution": ledger["execution"],
        "closed_trades": len(closed), "valid_quote_closes": valid,
        "wins": win, "win_rate_pct": round(100 * win / valid, 2) if valid else 0,
        "realized_pnl_sda": observed_pnl,
        "observed_quote_model_pnl_sda": observed_pnl,
        "invalid_quote_closes": len(invalid_rows),
        "conservative_risk_pnl_sda": conservative_pnl,
        "unverified_risk_assumption_sda": invalid_risk_pnl,
        "performance_basis": "VALID_OBSERVED_QUOTES_ONLY_NOT_EXECUTABLE_FILLS",
        "open_positions": len(ledger["positions"]),
        "open_notional_sda": round(POSITION_SDA * len(ledger["positions"]), 2),
        "rejected_entries": ledger["rejected_entries"],
        "qualification": "UNPROVEN_SAMPLE_DO_NOT_PROMOTE",
    }


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    at = datetime.now(timezone.utc)
    prepare_source(source)
    if LEDGER.exists():
        ledger = validate_ledger(json.loads(LEDGER.read_text(encoding="utf-8")))
    else:
        ledger = fresh_state(at)  # activation: never backfill historic SHADOW wins
    risk = paper_entry_risk_gate(at)
    result = step(ledger, source, at, risk)
    atomic_json(LEDGER, ledger)
    report = summary(ledger)
    report["latest_scan"] = result
    atomic_json(REPORT, report)
    print("TEMPORAL RECOVERY PAPER TRIAL:", json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
