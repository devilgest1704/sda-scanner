"""PAPER Champion vs Challenger optimizer (research + PAPER-only promotion).

Inputs (read-only): the PAPER research dataset (strategy_lab/paper_paths) and,
for older trades, the bounded price samples stored with PAPER closed trades.
Outputs: strategy_lab/paper_optimizer_report.json, strategy_lab/paper_optimizer_state.json
and, only when every gate passes, paper_champion_config.json (PAPER only).

Never imports or calls REAL trading code. SELL variants are research-only:
the PAPER champion SELL policy stays the shared Swing SELL V2 used by REAL.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paper_champion_config as champion_config
import swing_sell_v2 as policy
from tools import paper_research_dataset as dataset

REPORT_FILE = Path("strategy_lab/paper_optimizer_report.json")
STATE_FILE = Path("strategy_lab/paper_optimizer_state.json")
LOCK_FILE = Path("strategy_lab/.paper_optimizer.lock")
POSITIONS_FILE = Path("positions.json")
MISSED_PUMP_FILE = Path("strategy_lab/missed_pump_learning.json")
LOCK_STALE_SECONDS = 2 * 3600

FEE_RATE = 0.01
SLIPPAGE_RATE = 0.001
LEGACY_SAMPLE_CAP = 96
PAPER_STRATEGIES = {"temporal_recovery_tp4"}

GATES = {
    "min_total_trades": 30, "min_holdout_trades": 10, "min_tokens": 5,
    "max_single_trade_share": 0.40, "min_profit_factor": 1.15,
    "max_drawdown_ratio_vs_champion": 1.10, "max_drawdown_slack_sda": 0.5,
    "min_complete_path_share": 0.70, "max_censored_share": 0.30,
    "min_validation_trades": 3, "min_train_trades": 5,
    "shadow_min_trades": 20, "shadow_min_days": 7,
    "rollback_min_trades": 10, "rollback_max_drawdown_sda": 5.0,
    "min_improvement_sda": 0.05,
}
SCORE_WEIGHTS = {
    "net_pnl": 1.0, "profit_factor": 5.0, "expectancy": 2.0, "mfe_capture": 3.0,
    "max_drawdown": -1.0, "worst_trade": -0.5, "sell_count": -0.05,
    "fee_sensitivity": -2.0, "dominance_excess": -10.0, "low_trade_count": -0.5,
}

CHAMPION_SELL = {"emergency": -0.50, "partial_trigger": 0.25, "partial_size": 0.20,
                 "trailing": 0.15, "hard_tp": 0.50, "hard_tp_mode": "full"}
SELL_GRID = {"emergency": (-0.40, -0.50, -0.60), "partial_trigger": (0.20, 0.25, 0.30),
             "partial_size": (0.10, 0.20, 0.25, 0.33), "trailing": (0.10, 0.15, 0.20),
             "hard_tp_mode": ("full", "partial_runner", "trailing_only")}
BUY_FEATURES = {"min_score": "pump_score", "min_quality": "pump_quality", "min_impulse": "pump_change",
                "min_flow_1h": "net_1h", "min_volume_1h": "volume_1h", "min_trades_1h": "trades_1h",
                "min_buy_ratio": "buy_ratio", "max_m1h": "m1h"}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _num(value, default=None):
    try:
        result = float(value)
        return result if result == result else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- dataset

def _from_record(record):
    entry = record.get("entry") or {}
    observations = [{"at": o.get("at"), "price": _num(o.get("price"))} for o in record.get("observations") or []
                    if _num(o.get("price"), 0) > 0 and _iso(o.get("at"))]
    exits = record.get("exits") or []
    return {"id": record.get("position_id"), "token": entry.get("contract"), "symbol": entry.get("token"),
            "opened_at": entry.get("opened_at"), "fill_price": _num(entry.get("fill_price")),
            "investment": _num(entry.get("investment_sda"), 20.0), "metrics": entry.get("metrics") or {},
            "observations": observations, "status": record.get("status"),
            "actual_closed_at": record.get("closed_at"),
            "actual_pnl": sum(_num(x.get("pnl_sda"), 0) for x in exits),
            "complete": bool((record.get("data_quality") or {}).get("complete_from_entry")),
            "source": "paper_research_dataset"}


def _from_legacy(row):
    samples = row.get("pump_price_samples") or []
    observations = [{"at": s.get("at"), "price": _num(s.get("price_sda"))} for s in samples
                    if isinstance(s, dict) and _num(s.get("price_sda"), 0) > 0 and _iso(s.get("at"))]
    opened = _iso(row.get("opened_at"))
    first = _iso(observations[0]["at"]) if observations else None
    complete = (len(samples) < LEGACY_SAMPLE_CAP and opened is not None and first is not None
                and 0 <= (first - opened).total_seconds() <= 900)
    metrics = row.get("entry_metrics") if isinstance(row.get("entry_metrics"), dict) else {}
    return {"id": dataset.position_id(row.get("address"), row.get("opened_at")),
            "token": str(row.get("address") or "").lower(), "symbol": row.get("label"),
            "opened_at": row.get("opened_at"), "fill_price": _num(row.get("entry_price")),
            "investment": _num(row.get("investment_sda"), 20.0),
            "metrics": {k: _num(metrics.get(k)) for k in BUY_FEATURES.values()},
            "observations": observations, "status": "CLOSED", "actual_closed_at": row.get("closed_at"),
            "actual_pnl": _num(row.get("closed_profit_sda"), 0), "complete": complete,
            "source": "paper_closed_trade_samples"}


def load_trades(dataset_root=None, positions_file=None):
    """Chronological PAPER trades; dataset records win over legacy sample imports."""
    trades = {}
    try:
        ledger = json.loads(Path(positions_file or POSITIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    for row in (ledger.get("closed_trades") or []) if isinstance(ledger, dict) else []:
        if not isinstance(row, dict) or row.get("paper_strategy") not in PAPER_STRATEGIES:
            continue
        if str(row.get("status") or "").upper() != "CLOSED":
            continue
        trade = _from_legacy(row)
        if trade["fill_price"] and trade["opened_at"]:
            trades[trade["id"]] = trade
    for record in dataset.load_all(dataset_root):
        trade = _from_record(record)
        if trade["fill_price"] and trade["opened_at"]:
            trades[trade["id"]] = trade
    ordered = sorted(trades.values(), key=lambda t: (str(t["opened_at"]), t["id"]))
    forward_closed = [t for t in ordered if t["source"] == "paper_research_dataset" and t.get("status") == "CLOSED"]
    if len(forward_closed) >= GATES["min_total_trades"]:
        # Legacy paths were truncated by the retired swing_v1 exits; once enough full
        # Swing SELL V2 paths exist they are the only evidence used.
        ordered = [t for t in ordered if t["source"] == "paper_research_dataset"]
    for trade in ordered:
        start = _iso(trade["opened_at"])
        # No look-ahead: drop observations before entry and keep strict time order.
        trade["observations"] = sorted([o for o in trade["observations"] if _iso(o["at"]) >= start],
                                       key=lambda o: o["at"])
    return ordered


# ---------------------------------------------------------------- replay

def param_decide(state, pnl, price, params):
    """Parameterised Swing SELL variant; champion params must equal swing_sell_v2.decide."""
    if pnl <= params["emergency"]:
        return {"action": "FULL_SELL", "reason": "LOSS_LIMIT"}
    if params["hard_tp_mode"] == "full" and pnl >= params["hard_tp"]:
        return {"action": "FULL_SELL", "reason": "HARD_TP"}
    if params["hard_tp_mode"] == "partial_runner" and pnl >= params["hard_tp"] and not state.get("tp_partial_done"):
        return {"action": "PARTIAL_SELL", "reason": "HARD_TP_PARTIAL", "fraction": 0.5}
    if pnl >= params["partial_trigger"] and not state.get("profit_partial_done"):
        return {"action": "PARTIAL_SELL", "reason": "FIRST_PROFIT_PARTIAL", "fraction": params["partial_size"]}
    if state.get("trailing_armed"):
        peak = max(state.get("peak") or price, price)
        trailing = peak * (1 - params["trailing"])
        if price <= trailing:
            return {"action": "FULL_SELL", "reason": "TRAILING_STOP"}
        return {"action": "HOLD", "reason": "TRAILING_ACTIVE"}
    return {"action": "HOLD", "reason": "NO_THRESHOLD"}


def replay(trade, sell_params=None, fee_rate=FEE_RATE, slippage=SLIPPAGE_RATE):
    """Deterministic, observation-by-observation replay; uses no information after each observation."""
    champion = sell_params is None or sell_params == CHAMPION_SELL
    params = sell_params or CHAMPION_SELL
    entry, investment = trade["fill_price"], trade["investment"]
    unit_cost = entry * (1 + fee_rate)
    remaining, realized, fees, sells = 1.0, 0.0, 0.0, 0
    state = {"profit_partial_done": False, "trailing_armed": False, "peak": None, "tp_partial_done": False}
    mfe, mae, last, exited_at = 0.0, 0.0, None, None

    def sell(fraction, price):
        nonlocal remaining, realized, fees, sells
        part = remaining * fraction
        cost = investment * part
        gross = cost / entry * price * (1 - slippage)
        realized += gross * (1 - fee_rate) - cost * (1 + fee_rate)
        fees += gross * fee_rate + cost * fee_rate
        remaining -= part
        sells += 1

    for obs in trade["observations"]:
        price = obs["price"]
        pnl = price / unit_cost - 1
        mfe, mae, last = max(mfe, pnl), min(mae, pnl), obs
        if state["trailing_armed"]:
            state["peak"] = max(state["peak"] or price, price)
        if champion:
            pos = {"profit_partial_done": state["profit_partial_done"], "trailing_armed": state["trailing_armed"],
                   "highest_price_since_trailing_activation": state["peak"]}
            decision = policy.decide(pos, pnl, price)
        else:
            decision = param_decide(state, pnl, price, params)
        if decision["action"] == "FULL_SELL":
            sell(1.0, price)
            exited_at = obs["at"]
            break
        if decision["action"] == "PARTIAL_SELL":
            fraction = float(decision.get("fraction") or policy.PARTIAL_FRACTION)
            sell(fraction, price)
            if decision["reason"] == "HARD_TP_PARTIAL":
                state["tp_partial_done"] = True
            else:
                state["profit_partial_done"] = True
            state["trailing_armed"] = True
            state["peak"] = price
    unrealized = 0.0
    censored = False
    if exited_at is None and remaining > 1e-12 and last is not None:
        # Mark-to-market at the last OBSERVED price only (never a later or current price).
        cost = investment * remaining
        unrealized = cost / entry * last["price"] * (1 - slippage) * (1 - fee_rate) - cost * (1 + fee_rate)
        censored = trade.get("status") == "CLOSED"
    end = _iso(exited_at or (last or {}).get("at") or trade["opened_at"])
    hours = max(0.0, (end - _iso(trade["opened_at"])).total_seconds() / 3600) if end else 0.0
    net = realized + unrealized
    return {"id": trade["id"], "token": trade["token"], "opened_at": trade["opened_at"],
            "exit_at": exited_at or (last or {}).get("at"), "net": net, "realized": realized,
            "unrealized": unrealized, "censored": censored, "open": exited_at is None and not censored,
            "sells": sells, "fees": fees, "holding_hours": hours, "mfe": mfe, "mae": mae,
            "investment": investment, "complete": trade["complete"]}


# ---------------------------------------------------------------- metrics

def metrics(results, champion_dd=None):
    nets = [r["net"] for r in results]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    equity = peak = drawdown = 0.0
    for r in sorted(results, key=lambda r: str(r["exit_at"] or r["opened_at"])):
        equity += r["net"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    net = sum(nets)
    gross_loss = -sum(losses)
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0)
    best = max(nets) if nets else 0.0
    dominance = (best / net) if net > 0 and best > 0 else (1.0 if nets else 0.0)
    # Capture is only meaningful once a trade had a material favourable excursion.
    capture = [max(-2.0, min(2.0, r["net"] / (r["investment"] * (1 + FEE_RATE)) / r["mfe"]))
               for r in results if r["mfe"] >= 0.05]
    return {
        "trades": len(results), "tokens": len({r["token"] for r in results}),
        "net_pnl": round(net, 6), "realized_pnl": round(sum(r["realized"] for r in results), 6),
        "open_pnl": round(sum(r["unrealized"] for r in results), 6),
        "max_drawdown": round(drawdown, 6),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "profit_factor_infinite": profit_factor == float("inf"),
        "expectancy": round(net / len(nets), 6) if nets else 0.0,
        "win_rate": round(len(wins) / len(nets) * 100, 2) if nets else 0.0,
        "avg_winner": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avg_loser": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "best_trade": round(best, 6), "worst_trade": round(min(nets), 6) if nets else 0.0,
        "sell_count": sum(r["sells"] for r in results),
        "avg_holding_hours": round(sum(r["holding_hours"] for r in results) / len(results), 3) if results else 0.0,
        "exposure_hours": round(sum(r["holding_hours"] for r in results), 3),
        "capital_utilization_sda_hours": round(sum(r["holding_hours"] * r["investment"] for r in results), 3),
        "modeled_costs": round(sum(r["fees"] for r in results), 6),
        "calmar": round(net / drawdown, 4) if drawdown > 0 else None,
        "mfe_capture": round(sum(capture) / len(capture), 4) if capture else None,
        "worst_mae_pct": round(min((r["mae"] for r in results), default=0.0) * 100, 3),
        "single_trade_share": round(dominance, 4),
        "censored": sum(r["censored"] for r in results), "open_positions": sum(r["open"] for r in results),
        "complete_paths": sum(r["complete"] for r in results),
    }


def composite_score(m, fee_sensitivity=0.0):
    pf = 2.0 if m["profit_factor_infinite"] else min(m["profit_factor"] or 0.0, 3.0)
    w = SCORE_WEIGHTS
    return round(w["net_pnl"] * m["net_pnl"] + w["profit_factor"] * (pf - 1) + w["expectancy"] * m["expectancy"]
                 + w["mfe_capture"] * (m["mfe_capture"] or 0.0) + w["max_drawdown"] * m["max_drawdown"]
                 + w["worst_trade"] * abs(min(m["worst_trade"], 0)) + w["sell_count"] * m["sell_count"]
                 + w["fee_sensitivity"] * fee_sensitivity
                 + w["dominance_excess"] * max(0.0, m["single_trade_share"] - GATES["max_single_trade_share"])
                 + w["low_trade_count"] * max(0, 10 - m["trades"]), 4)


def chronological_splits(trades):
    """60/20/20 by entry time; never shuffled."""
    n = len(trades)
    a, b = int(n * 0.6), int(n * 0.8)
    return {"train": trades[:a], "validation": trades[a:b], "holdout": trades[b:]}


def walk_forward(trades, folds=4):
    size = max(1, len(trades) // (folds + 1))
    return [(trades[:size * (k + 1)], trades[size * (k + 1):size * (k + 2)]) for k in range(folds)
            if trades[size * (k + 1):size * (k + 2)]]


def split_summary(trades):
    stamps = [t["opened_at"] for t in trades]
    return {"trades": len(trades), "tokens": len({t["token"] for t in trades}),
            "from": min(stamps) if stamps else None, "to": max(stamps) if stamps else None,
            "complete_paths": sum(t["complete"] for t in trades),
            "path_completeness": round(sum(t["complete"] for t in trades) / len(trades), 4) if trades else 0.0}


# ---------------------------------------------------------------- challengers

def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def generate_challengers(train):
    """SELL variants (research-only) and stricter BUY filters derived from TRAIN data only."""
    out = []
    keys = list(SELL_GRID)
    for combo in itertools.product(*(SELL_GRID[k] for k in keys)):
        params = dict(CHAMPION_SELL, **dict(zip(keys, combo)))
        if params == CHAMPION_SELL:
            continue
        out.append({"id": "sell-" + _hash(params), "kind": "sell", "sell_params": params, "buy_filters": {}})
    for key, field in BUY_FEATURES.items():
        values = sorted(v for v in (_num((t["metrics"] or {}).get(field)) for t in train) if v is not None)
        if len(values) < 4:
            continue
        for q in (0.25, 0.5):
            idx = int(len(values) * q) if key.startswith("min") else int(len(values) * (1 - q))
            threshold = round(values[min(idx, len(values) - 1)], 4)
            filters = {key: threshold}
            out.append({"id": "buy-" + _hash(filters), "kind": "buy", "sell_params": None, "buy_filters": filters})
    return out


def _select(trades, filters):
    return [t for t in trades if not champion_config.buy_veto(t["metrics"] or {}, {"buy_filters": filters})]


def evaluate_candidate(candidate, trades, fee_rate=FEE_RATE):
    chosen = _select(trades, candidate["buy_filters"]) if candidate["buy_filters"] else trades
    results = [replay(t, candidate["sell_params"], fee_rate) for t in chosen]
    return results


def _fee_sensitivity(candidate, trades):
    base = metrics(evaluate_candidate(candidate, trades))["net_pnl"]
    double = metrics(evaluate_candidate(candidate, trades, FEE_RATE * 2))["net_pnl"]
    return round((base - double) / max(abs(base), 1.0), 4), double


def _pf(m):
    return float("inf") if m["profit_factor_infinite"] else (m["profit_factor"] or 0.0)


def gate_train(c, ch):
    reasons = []
    if c["trades"] < GATES["min_train_trades"]:
        reasons.append(f"only {c['trades']} train trades")
    if c["net_pnl"] <= ch["net_pnl"] + GATES["min_improvement_sda"]:
        reasons.append(f"train net {c['net_pnl']:+.2f} not above champion {ch['net_pnl']:+.2f}")
    if _pf(c) < 1.0:
        reasons.append("train profit factor < 1.0")
    return not reasons, reasons


def gate_validation(c, ch):
    reasons = []
    if c["trades"] < GATES["min_validation_trades"]:
        reasons.append(f"only {c['trades']} validation trades")
    if c["net_pnl"] <= 0:
        reasons.append(f"validation PnL {c['net_pnl']:+.2f} not positive")
    if _pf(c) <= GATES["min_profit_factor"]:
        reasons.append(f"validation profit factor {c['profit_factor']} <= {GATES['min_profit_factor']}")
    if c["expectancy"] <= 0:
        reasons.append("validation expectancy not positive")
    if c["net_pnl"] <= ch["net_pnl"] + GATES["min_improvement_sda"]:
        reasons.append("validation PnL not above champion")
    return not reasons, reasons


def gate_holdout(c, ch):
    reasons = []
    if c["trades"] < GATES["min_holdout_trades"]:
        reasons.append(f"only {c['trades']} holdout trades (need {GATES['min_holdout_trades']})")
    if c["tokens"] < GATES["min_tokens"]:
        reasons.append(f"only {c['tokens']} holdout tokens (need {GATES['min_tokens']})")
    if c["net_pnl"] <= 0:
        reasons.append(f"holdout PnL {c['net_pnl']:+.2f} not positive")
    if _pf(c) <= GATES["min_profit_factor"]:
        reasons.append(f"holdout profit factor {c['profit_factor']} <= {GATES['min_profit_factor']}")
    if c["expectancy"] <= 0 or c["expectancy"] <= ch["expectancy"]:
        reasons.append("holdout expectancy not positive and better than champion")
    if c["net_pnl"] <= ch["net_pnl"] + GATES["min_improvement_sda"]:
        reasons.append("holdout PnL not above champion")
    if c["single_trade_share"] > GATES["max_single_trade_share"]:
        reasons.append(f"{c['single_trade_share'] * 100:.0f}% of PnL from one trade")
    limit = ch["max_drawdown"] * GATES["max_drawdown_ratio_vs_champion"] + GATES["max_drawdown_slack_sda"]
    if c["max_drawdown"] > limit:
        reasons.append(f"drawdown {c['max_drawdown']:.2f} worse than champion {ch['max_drawdown']:.2f}")
    return not reasons, reasons


def gate_data_quality(trades, results):
    reasons = []
    n = len(trades)
    if n < GATES["min_total_trades"]:
        reasons.append(f"only {n} completed PAPER trades (need {GATES['min_total_trades']})")
    tokens = len({t["token"] for t in trades})
    if tokens < GATES["min_tokens"]:
        reasons.append(f"only {tokens} distinct tokens")
    complete = sum(t["complete"] for t in trades) / n if n else 0.0
    if complete < GATES["min_complete_path_share"]:
        reasons.append(f"complete price paths {complete * 100:.0f}% (need {GATES['min_complete_path_share'] * 100:.0f}%)")
    censored = sum(r["censored"] for r in results) / len(results) if results else 0.0
    if censored > GATES["max_censored_share"]:
        reasons.append(f"{censored * 100:.0f}% of champion replays end before an exit (censored)")
    return not reasons, reasons


def gate_robustness(candidate, trades, fee_double_net):
    reasons = []
    if fee_double_net <= 0:
        reasons.append(f"net PnL with doubled costs {fee_double_net:+.2f} not positive")
    folds = walk_forward(trades)
    positive = 0
    for _train, test in folds:
        c = metrics(evaluate_candidate(candidate, test))
        positive += c["net_pnl"] > 0
    if folds and positive < (2 * len(folds) + 2) // 3:
        reasons.append(f"positive in only {positive}/{len(folds)} walk-forward folds")
    return not reasons, reasons


def shadow_evaluation(state, trades, now):
    """Forward Shadow: challenger vs champion on trades opened after enrollment (identical opportunities)."""
    results = []
    for entry in state.get("shadow", []):
        started = _iso(entry.get("started_at"))
        candidate = entry["candidate"]
        forward = [t for t in trades if started and _iso(t["opened_at"]) >= started]
        closed = [t for t in forward if t.get("status") == "CLOSED"]
        champ = metrics([replay(t) for t in closed])
        chall = metrics(evaluate_candidate(candidate, closed))
        days = (now - started).total_seconds() / 86400 if started else 0.0
        reasons = []
        if chall["trades"] < GATES["shadow_min_trades"]:
            reasons.append(f"only {chall['trades']} completed Shadow trades (need {GATES['shadow_min_trades']})")
        if days < GATES["shadow_min_days"]:
            reasons.append(f"Shadow running {days:.1f} days (need {GATES['shadow_min_days']})")
        if chall["net_pnl"] <= champ["net_pnl"] + GATES["min_improvement_sda"]:
            reasons.append("Shadow PnL not above champion over the same period")
        if _pf(chall) < _pf(champ):
            reasons.append("Shadow profit factor worse than champion")
        results.append({"candidate_id": candidate["id"], "started_at": entry.get("started_at"), "days": round(days, 2),
                        "champion": champ, "challenger": chall, "passed": not reasons, "reasons": reasons})
    return results


def rollback_check(config, trades, now):
    promotion = config.get("promotion") or {}
    promoted = _iso(promotion.get("at"))
    if not promoted or not config.get("previous"):
        return {"ready": bool(config.get("previous")), "triggered": False, "reasons": [],
                "previous_version": (config.get("previous") or {}).get("version")}
    forward = [t for t in trades if _iso(t["opened_at"]) >= promoted and t.get("status") == "CLOSED"]
    m = metrics([replay(t) for t in forward])
    reasons = []
    if m["trades"] >= GATES["rollback_min_trades"]:
        recent = metrics([replay(t) for t in forward[-GATES["rollback_min_trades"]:]])
        if recent["expectancy"] < 0:
            reasons.append(f"rolling expectancy {recent['expectancy']:+.3f} SDA negative")
        if m["max_drawdown"] > GATES["rollback_max_drawdown_sda"]:
            reasons.append(f"forward drawdown {m['max_drawdown']:.2f} SDA > {GATES['rollback_max_drawdown_sda']}")
    return {"ready": True, "triggered": bool(reasons), "reasons": reasons, "forward": m,
            "previous_version": config["previous"].get("version")}


# ---------------------------------------------------------------- orchestration

def _atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


class OptimizerLocked(RuntimeError):
    pass


def _research_counters():
    """Missed-pump research counters (observational, never a promotion input)."""
    source = _load_json(MISSED_PUMP_FILE, {})
    return {"status": source.get("status"), "observed_candidates": source.get("sample_count"),
            "excluded_inputs": source.get("excluded_from_causal_learning"),
            "missed_pump_examples": source.get("missed_pump_examples"),
            "peak_240m_examples": source.get("potential_240m_peak_examples"),
            "tested_rules": source.get("tested_rules"), "updated_at": source.get("updated_at")}


def _lane_research(now, lane_files, positions_file, dataset_root):
    """Research-only BUY-lane challengers; never promoted, never changes PAPER or REAL."""
    from tools import lane_research
    try:
        return lane_research.research(now, positions_file=positions_file or POSITIONS_FILE,
                                      dataset_dir=dataset_root or dataset.DATASET_DIR, **lane_files)
    except Exception as exc:  # research must never block the Champion report
        return {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"[:300],
                "decision": "KEEP CHAMPION", "promotion_applied": False}


def acquire_lock(path=LOCK_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            raise OptimizerLocked(f"optimizer already running (lock age {age:.0f}s)")
        path.unlink(missing_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return path


def _comparison(champ, chall):
    keys = ("net_pnl", "realized_pnl", "open_pnl", "max_drawdown", "profit_factor", "expectancy", "win_rate",
            "avg_winner", "avg_loser", "best_trade", "worst_trade", "trades", "avg_holding_hours",
            "sell_count", "modeled_costs")
    rows = []
    for key in keys:
        a = champ.get(key) if champ else None
        b = chall.get(key) if chall else None
        diff = round(b - a, 6) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        rows.append({"metric": key, "champion": a, "challenger": b if chall else None, "difference": diff})
    return rows


def run(now=None, dataset_root=None, positions_file=None, report_file=REPORT_FILE, state_file=STATE_FILE,
        config_file=None, apply=True, lane_state_file=None, lane_archive_file=None, counterfactual_gate=None):
    from tools import lane_research
    lane_files = {"state_file": lane_state_file or lane_research.STATE_FILE,
                  "archive_file": lane_archive_file or lane_research.CANDIDATE_ARCHIVE}
    if counterfactual_gate is None:
        def counterfactual_gate(buy_filters, when):
            return lane_research.counterfactual_gate(buy_filters, when, **lane_files)
    now = now or _now()
    config = champion_config.load(config_file)
    state = _load_json(state_file, {})
    state.setdefault("shadow", [])
    trades = load_trades(dataset_root, positions_file)
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    splits = chronological_splits(closed)
    champion = {"id": "champion", "kind": "champion", "version": config.get("version"),
                "sell_params": None, "buy_filters": dict(config.get("buy_filters") or {})}
    champ_all_results = evaluate_candidate(champion, closed)
    champ_split = {k: metrics(evaluate_candidate(champion, v)) for k, v in splits.items()}
    champ_all = metrics(champ_all_results)
    dq_ok, dq_reasons = gate_data_quality(closed, champ_all_results)

    candidates = generate_challengers(splits["train"])
    evaluated = []
    for candidate in candidates:
        train = metrics(evaluate_candidate(candidate, splits["train"]))
        ok, reasons = gate_train(train, champ_split["train"])
        evaluated.append({"candidate": candidate, "train": train, "train_passed": ok, "reasons": reasons,
                          "train_score": composite_score(train)})
    survivors = sorted([e for e in evaluated if e["train_passed"]], key=lambda e: e["train_score"], reverse=True)[:10]
    for e in survivors:
        e["validation"] = metrics(evaluate_candidate(e["candidate"], splits["validation"]))
        e["validation_passed"], val_reasons = gate_validation(e["validation"], champ_split["validation"])
        e["reasons"] = val_reasons
        e["validation_score"] = composite_score(e["validation"])
    validated = sorted([e for e in survivors if e.get("validation_passed")],
                       key=lambda e: e["validation_score"], reverse=True)
    best = validated[0] if validated else (survivors[0] if survivors else
                                           (max(evaluated, key=lambda e: e["train_score"]) if evaluated else None))
    if best is not None and best.get("validation_passed"):
        # The holdout is evaluated only for the single candidate already chosen on train + validation.
        best["holdout"] = metrics(evaluate_candidate(best["candidate"], splits["holdout"]))
        best["holdout_passed"], hold_reasons = gate_holdout(best["holdout"], champ_split["holdout"])
        sensitivity, double_net = _fee_sensitivity(best["candidate"], splits["validation"] + splits["holdout"])
        best["fee_sensitivity"] = sensitivity
        best["robustness_passed"], robust_reasons = gate_robustness(best["candidate"], closed, double_net)
        best["reasons"] = hold_reasons + robust_reasons

    shadow = shadow_evaluation(state, trades, now)
    decision, status, reasons, promotion_applied = "KEEP CHAMPION", "WAITING_FOR_DATA", [], False
    if not dq_ok:
        decision, status, reasons = "KEEP CHAMPION", "INSUFFICIENT_DATA", dq_reasons + (best or {}).get("reasons", [])
    elif best is None:
        status, reasons = "NO_CHALLENGER", ["no challenger could be generated from the train split"]
    elif not best["train_passed"]:
        decision, status, reasons = "REJECT CHALLENGER", "REJECTED_TRAIN", best["reasons"]
    elif not best.get("validation_passed"):
        decision, status, reasons = "REJECT CHALLENGER", "REJECTED_VALIDATION", best["reasons"]
    elif not best.get("holdout_passed") or not best.get("robustness_passed"):
        decision, status, reasons = "REJECT CHALLENGER", "REJECTED_HOLDOUT", best["reasons"]
    elif best["candidate"]["kind"] == "sell":
        decision, status = "KEEP CHAMPION", "RESEARCH_ONLY"
        reasons = ["SELL variant passed history but PAPER SELL must stay the shared Swing SELL V2 used by REAL"]
    else:
        enrolled = next((s for s in shadow if s["candidate_id"] == best["candidate"]["id"]), None)
        if enrolled is None:
            state["shadow"].append({"candidate": best["candidate"], "started_at": now.isoformat()})
            status, reasons = "SHADOW", ["historical gates passed; forward Shadow started"]
        elif not enrolled["passed"]:
            status, reasons = "SHADOW", enrolled["reasons"]
        else:
            # Subset replay ignores freed slots; the full-stream capacity replay must agree.
            cf_ok, cf_reasons = counterfactual_gate(best["candidate"]["buy_filters"], now)
            best["counterfactual_passed"] = cf_ok
            if not cf_ok:
                status, reasons = "SHADOW", ["counterfactual capacity replay: " + r for r in cf_reasons]
        if enrolled is not None and enrolled["passed"] and best.get("counterfactual_passed"):
            status = "PROMOTION_READY"
            if apply:
                version = f"PAPER-CHAMPION-{best['candidate']['id']}"
                champion_config.promote({**best["candidate"], "version": version},
                                        {"holdout": best["holdout"], "shadow": enrolled["challenger"]},
                                        "all train/validation/holdout/robustness/shadow gates passed",
                                        config_file)
                decision, status, promotion_applied = "PROMOTE TO PAPER", "PROMOTED", True
                state["shadow"] = [s for s in state["shadow"] if s["candidate"]["id"] != best["candidate"]["id"]]

    rollback = rollback_check(champion_config.load(config_file), closed, now)
    rollback_applied = False
    if rollback["triggered"] and apply and not promotion_applied:
        rollback_applied = champion_config.rollback("; ".join(rollback["reasons"]), config_file) is not None

    def view(e):
        if e is None:
            return None
        c = e["candidate"]
        return {"id": c["id"], "kind": c["kind"], "sell_params": c["sell_params"], "buy_filters": c["buy_filters"],
                "train": e.get("train"), "validation": e.get("validation"), "holdout": e.get("holdout"),
                "fee_sensitivity": e.get("fee_sensitivity"),
                "gates": {"train": e.get("train_passed"), "validation": e.get("validation_passed"),
                          "holdout": e.get("holdout_passed"), "robustness": e.get("robustness_passed"),
                          "counterfactual": e.get("counterfactual_passed")},
                "reasons": e.get("reasons", [])}

    best_view = view(best)
    best_all = metrics(evaluate_candidate(best["candidate"], closed)) if best else None
    report = {
        "schema": 1, "paper_only": True, "real_trading": False, "generated_at": now.isoformat(),
        "mode": "CHAMPION_VS_CHALLENGER", "decision": decision, "status": status, "reasons": reasons,
        "promotion_applied": promotion_applied, "rollback_applied": rollback_applied, "real_promotion": "NEVER",
        "champion": {"version": config.get("version"), "sell_policy": "swing_v2",
                     "buy_filters": config.get("buy_filters") or {}, "all": champ_all, "splits": champ_split},
        "challenger": best_view, "challenger_all": best_all,
        "comparison": _comparison(champ_all, best_all), "comparison_scope": "all completed trades, identical opportunities",
        "gates": {"data_quality": dq_ok, "data_quality_reasons": dq_reasons,
                  "train": (best_view or {}).get("gates", {}).get("train"),
                  "validation": (best_view or {}).get("gates", {}).get("validation"),
                  "holdout": (best_view or {}).get("gates", {}).get("holdout"),
                  "robustness": (best_view or {}).get("gates", {}).get("robustness"),
                  "shadow": any(s["passed"] for s in shadow) if shadow else None,
                  "thresholds": GATES},
        "splits": {k: split_summary(v) for k, v in splits.items()},
        "dataset": {"positions": len(trades), "closed_trades": len(closed),
                    "complete_paths": sum(t["complete"] for t in trades),
                    "tokens": len({t["token"] for t in trades}),
                    "from": trades[0]["opened_at"] if trades else None,
                    "to": trades[-1]["opened_at"] if trades else None,
                    "sources": {s: sum(t["source"] == s for t in trades)
                                for s in ("paper_research_dataset", "paper_closed_trade_samples")}},
        "candidates_tested": len(evaluated), "candidates_passed_train": len(survivors),
        "candidates_passed_validation": len(validated),
        "shadow": shadow, "rollback": {k: v for k, v in rollback.items() if k != "forward"},
        "score_weights": SCORE_WEIGHTS,
        "research": _research_counters(),
        "lane_research": _lane_research(now, lane_files, positions_file, dataset_root),
    }
    state.update({"last_run_at": now.isoformat(), "last_decision": decision, "last_status": status})
    _atomic_write(state_file, state)
    _atomic_write(report_file, report)
    return report


def main(argv=None):
    try:
        lock = acquire_lock()
    except OptimizerLocked as exc:
        print(f"PAPER OPTIMIZER SKIPPED: {exc}")
        return 0
    try:
        report = run()
        print(json.dumps({k: report[k] for k in ("decision", "status", "reasons", "dataset")}, indent=2, default=str))
        from tools import lane_research
        print(lane_research.render_text(report.get("lane_research")))
        return 0
    finally:
        Path(lock).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
