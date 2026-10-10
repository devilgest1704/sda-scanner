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
from datetime import datetime, timezone
from pathlib import Path
from tools.forward_shadow_agent import POLICIES, when, finite

STATE_PATH = Path("strategy_lab/forward_shadow_state.json")
CONFIG_PATH = Path("v30_agent_config.json")
STATS_PATH = Path("paper_stats.json")
POSITIONS_PATH = Path("positions.json")
REPORT_PATH = Path("strategy_lab/autonomous_agent_report.json")
MIN_TRAIN = 30
MIN_HOLDOUT = 15
MIN_UNIQUE = 6
MIN_HOLDOUT_PNL = 10.0
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
    if config.get("version") != "V30-AUTO-CONFIG-1":
        raise ValueError("Unrecognized Champion config")
    current = config.get("filters")
    if not isinstance(current, dict) or set(POLICIES["baseline"]) - set(current):
        raise ValueError("Invalid Champion filters")
    model = {
        "version": "FORWARD-PAPER-AGENT-1",
        "updated_at": now.isoformat(),
        "mode": "PAPER_ONLY",
        "objective": "maximize accumulated net SDA subject to observed quote risk",
        "real_trading": False, "paper_config_automatic": True,
        "shadow_method": "forward observed quote fills, estimated friction, NOT real market execution",
        "source_last_scan_at": state.get("updated_at"),
        "strategies": {},
        "champion": {"generation": config.get("generation", 0), "filters": current},
        "promotion_applied": False,
        "rollback_applied": False,
    }
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

    strategies = state.get("strategies", {})
    if not isinstance(strategies, dict) or any(name not in strategies for name in POLICIES):
        raise ValueError("Incomplete forward strategy journals")

    all_rows = {name: forward_rows(strategies[name]) for name in POLICIES}
    unique_times = sorted({r["closed_at"] for rows in all_rows.values() for r in rows})
    if len(unique_times) < MIN_TRAIN + MIN_HOLDOUT:
        model["status"] = "COLLECTING_FORWARD_TRADES"
        model["reason"] = "Not enough separately observed chronological outcomes"
        for name, rows in all_rows.items():
            model["strategies"][name] = {"all": summary(rows)}
        return model, config

    boundary = unique_times[int(len(unique_times)*0.7)]
    candidates = []
    baseline_train = baseline_holdout = None
    for name, rows in all_rows.items():
        train = [r for r in rows if r["closed_at"] < boundary]
        holdout = [r for r in rows if r["closed_at"] >= boundary]
        train_stats = summary(train)
        model["strategies"][name] = {
            "all": summary(rows), "train": train_stats,
            "holdout_sample_count": len(holdout),
            # No holdout P/L evaluated before selecting a winner.
        }
        if name == "baseline":
            baseline_train = train_stats
            baseline_holdout = holdout
        if (name != "baseline" and not name.startswith("research_watch")
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
    incumbent = summary(baseline_holdout)
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
