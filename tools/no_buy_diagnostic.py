#!/usr/bin/env python3
"""Explain missing PAPER BUY decisions from observed scanner evidence only.

No network, no orders, no decisions, no modified trading config.
Always distinguish risk-circuit-only blocks, absent entry signals, and stale
scanner/market observations. A reported no-BUY is not a trading bug by itself.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from tools.paper_loss_circuit import evaluate_closed


def parsed_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def read_json(path):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return obj


def market_coverage(market):
    """Count only actually observed trades within the market snapshot."""
    as_of = parsed_time(market.get("updated_at"))
    tokens = market.get("tokens")
    if not isinstance(tokens, dict) or as_of is None:
        return {"status": "INVALID_MARKET_SNAPSHOT"}
    out = {
        "status": "OK", "market_at": as_of.isoformat(),
        "tokens": 0, "quotes_last_5m": 0, "quotes_last_15m": 0,
        "quotes_last_60m": 0, "fresh_with_8_trades_1h": 0,
        "fresh_with_12_trades_1h": 0,
        "fresh_m15_ge_1_5pct": 0,
        "fresh_m15_ge_1_5pct_and_8_trades": 0,
        "fresh_m15_ge_1_5pct_8_trades_and_net_flow_200": 0,
    }
    for token in tokens.values():
        if not isinstance(token, dict):
            continue
        analysis = token.get("analysis", token)
        if not isinstance(analysis, dict) or number(analysis.get("price_in_sda")) is None:
            continue
        out["tokens"] += 1
        ts = parsed_time(analysis.get("last_transaction"))
        if ts is None:
            continue
        seconds = (as_of-ts).total_seconds()
        if seconds < 0:
            continue
        if seconds <= 3600:
            out["quotes_last_60m"] += 1
        if seconds <= 900:
            out["quotes_last_15m"] += 1
        if seconds > 300:
            continue
        out["quotes_last_5m"] += 1
        flow = analysis.get("flow", {})
        hour = flow.get("1h", {}) if isinstance(flow, dict) else {}
        hour = hour if isinstance(hour, dict) else {}
        momentum = analysis.get("momentum", {})
        momentum = momentum if isinstance(momentum, dict) else {}
        trades = number(hour.get("transactions")) or 0
        net = number(hour.get("net_flow")) or 0
        m15 = number(momentum.get("15m_pct"))
        if trades >= 8:
            out["fresh_with_8_trades_1h"] += 1
        if trades >= 12:
            out["fresh_with_12_trades_1h"] += 1
        if m15 is not None and m15 >= 1.5:
            out["fresh_m15_ge_1_5pct"] += 1
            if trades >= 8:
                out["fresh_m15_ge_1_5pct_and_8_trades"] += 1
                if net >= 200:
                    out["fresh_m15_ge_1_5pct_8_trades_and_net_flow_200"] += 1
    return out


def diagnose(market, execution, ledger, at=None):
    now = at or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("The audit requires a timezone-aware timestamp")
    markets = market_coverage(market)
    funnels = execution.get("funnels") if isinstance(execution, dict) else None
    funnel = funnels[-1] if isinstance(funnels, list) and funnels else None
    closed = ledger.get("closed_trades") if isinstance(ledger, dict) else None
    circuit = evaluate_closed(closed, now)
    latest_closed = None
    if isinstance(closed, list):
        seen = [parsed_time(t.get("closed_at"))
                for t in closed if isinstance(t, dict)
                and t.get("status") == "CLOSED"]
        seen = [t for t in seen if t and t <= now]
        latest_closed = max(seen).isoformat() if seen else None

    result = {
        "mode": "READ_ONLY_PAPER_NO_BUY_DIAGNOSTIC",
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "market": markets,
        "paper_loss_circuit": circuit,
        "ledger_closed_total": len(closed) if isinstance(closed, list) else None,
        "last_paper_close_at": latest_closed,
        "funnel": {"status": "MISSING"},
        "assessment": "CANNOT_ASSESS_WITHOUT_CURRENT_DECISION_FUNNEL",
        "paper_buy_rules_modified": False,
        "real_trading": False,
    }
    if not isinstance(funnel, dict):
        return result
    stamp = parsed_time(funnel.get("at"))
    source = parsed_time(markets.get("market_at"))
    age_minutes = (now-stamp).total_seconds()/60 if stamp else None
    aligned = (stamp is not None and source is not None
               and 0 <= (stamp-source).total_seconds() <= 900)
    rows = funnel.get("candidate_snapshots")
    rows = rows if isinstance(rows, list) else []
    gates = Counter()
    phase = Counter()
    risk_only = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        phase[str(row.get("phase") or "UNKNOWN")] += 1
        blockers = {str(x) for x in (row.get("gates") or [])}
        for name in blockers:
            gates[name] += 1
        if blockers == {"loss_circuit"}:
            risk_only += 1
    fresh_gates = funnel.get("fresh_gate_counts")
    if not isinstance(fresh_gates, dict):
        fresh_gates = {}
    result["funnel"] = {
        "status": ("CURRENT" if (age_minutes is not None and
                              0 <= age_minutes <= 30 and aligned)
                   else "STALE_OR_NOT_TIME_ALIGNED"),
        "at": stamp.isoformat() if stamp else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "aligned_to_market": bool(aligned),
        "tokens_scored": funnel.get("tokens"),
        "fresh_tokens": funnel.get("fresh"),
        "champion_ready": funnel.get("champion_ready"),
        "shadow_ready": funnel.get("shadow_ready"),
        "sampled_rows": len(rows),
        "sampled_phases": dict(phase),
        "sampled_rejections": dict(gates.most_common(12)),
        "fresh_rejections": fresh_gates,
        "temporal_lane_rejections": funnel.get("temporal_lane_rejections") if isinstance(funnel.get("temporal_lane_rejections"), dict) else {},
        "temporal_lane_ready": funnel.get("temporal_lane_ready") if isinstance(funnel.get("temporal_lane_ready"), dict) else {},
        "risk_only_rejected_rows": risk_only,
        "would_buy_if_only_loss_circuit_cleared": risk_only > 0,
        "top_near_ready": (funnel.get("near_ready") or [])[:5],
    }
    if result["funnel"]["status"] != "CURRENT":
        result["assessment"] = "STALE_OR_UNALIGNED_SCANNER_OBSERVATIONS"
    elif isinstance(funnel.get("champion_ready"), int) and funnel["champion_ready"] > 0:
        result["assessment"] = "CHAMPION_BUY_READY_CHECK_EXECUTION_CAPACITY"
    elif risk_only:
        result["assessment"] = "ONLY_PAPER_LOSS_CIRCUIT_BLOCKED_SOME_SIGNALS"
    elif markets.get("status") == "OK" and markets.get("quotes_last_5m", 0) == 0:
        result["assessment"] = "NO_FRESH_MARKET_QUOTE"
    elif isinstance(funnel.get("champion_ready"), int) and funnel["champion_ready"] == 0:
        # A loss cooldown AND no technical entry signal are independent
        # reasons. Do not misreport that lifting the risk guard would BUY.
        result["assessment"] = (
            "NO_ELIGIBLE_BUY_SIGNALS_AND_LOSS_COOLDOWN"
            if circuit.get("blocked") and circuit.get("status") == "LOSS_COOLDOWN"
            else "NO_ELIGIBLE_BUY_SIGNALS_OBSERVED")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="market_analysis.json")
    ap.add_argument("--execution", default="v30_execution_state.json")
    ap.add_argument("--positions", default="positions.json")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    result = diagnose(read_json(args.market), read_json(args.execution),
                      read_json(args.positions))
    serialized = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(serialized+"\n", encoding="utf-8")
    print("NO BUY DIAGNOSTIC: "+json.dumps({
        "assessment": result["assessment"],
        "funnel": result["funnel"],
        "market": result["market"],
        "loss_circuit": result["paper_loss_circuit"],
        "last_close": result["last_paper_close_at"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
