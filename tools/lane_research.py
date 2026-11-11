"""PAPER BUY-lane research: SHADOW signal recorder + counterfactual capacity replay.

Research only. Nothing here changes the PAPER Champion, PAPER BUY/SELL decisions,
REAL mirroring or REAL trading. Lane challengers are never promoted by this module.

* ``record_scan`` (called from the scanner's diagnostic funnel) persists every
  eligible PAPER BUY signal with its lane and entry features, PAPER/REAL capacity
  at that moment, and forward observed quotes for every token that produced a
  signal. Hypothetical positions are therefore tracked forward in SHADOW with
  real observed quotes; nothing is inferred for tokens without observations.
* ``simulate`` replays the complete chronological signal stream under identical
  capital, 10 slots, 20 SDA sizing, 1 BUY per scan, 6 h re-entry cooldown, the
  shared Swing SELL V2 and the optimizer's modeled costs. A rejected signal
  frees a slot that a later signal may take; exits release slots only once the
  exit quote has been observed (no look-ahead).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("strategy_lab/lane_research_state.json")
CANDIDATE_ARCHIVE = Path("strategy_lab/candidate_outcomes.json")
POSITIONS_FILE = Path("positions.json")
REAL_STATE_FILE = Path("real_trade_state.json")
DATASET_DIR = Path("strategy_lab/paper_paths")

SLOTS = 10
INVESTMENT_SDA = 20.0
MAX_BUYS_PER_SCAN = 1
COOLDOWN_HOURS = 6.0
SCAN_SECONDS = 60
TRACK_DAYS = 30
MAX_SIGNALS = 20000
MAX_PATH_POINTS = 4000
SINGLE_QUOTE = "swing_single_quote"
FEATURES = ("score", "quality", "impulse", "flow_15m", "flow_1h", "volume_1h", "trades_1h",
            "buy_ratio", "m15", "m1h", "phase", "data_age_sec")
# Candidate features expressed in the champion-config filter vocabulary (buy_veto).
VETO_FIELDS = {"pump_score": "score", "pump_quality": "quality", "pump_change": "impulse",
               "net_1h": "flow_1h", "flow_15m": "flow_15m", "volume_1h": "volume_1h",
               "trades_1h": "trades_1h", "buy_ratio": "buy_ratio", "m1h": "m1h", "m15": "m15"}
# Quality/activity keys of the optimizer BUY search space used for STRICT SINGLE-QUOTE.
STRICT_KEYS = {"min_score": "score", "min_quality": "quality", "min_trades_1h": "trades_1h",
               "min_volume_1h": "volume_1h", "min_flow_1h": "flow_1h"}


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


def _load(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- capacity snapshot

def paper_open_count(positions_file=POSITIONS_FILE):
    ledger = _load(positions_file, {})
    positions = ledger.get("positions") if isinstance(ledger.get("positions"), dict) else {}
    return sum(1 for p in positions.values()
               if isinstance(p, dict) and str(p.get("status") or "OPEN").upper() == "OPEN")


def real_capacity(real_state_file=REAL_STATE_FILE):
    """Read-only REAL slot/capital usage (JSON only; never imports REAL code)."""
    state = _load(real_state_file, {})
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    pending = state.get("pending_buys") if isinstance(state.get("pending_buys"), dict) else {}
    max_open = int(_num(os.environ.get("REAL_MAX_OPEN_POSITIONS"), 10))
    capital = _num(os.environ.get("REAL_TRADING_CAPITAL_SDA"), 200.0)
    size = _num(os.environ.get("REAL_POSITION_DEFAULT_SDA"), 20.0)
    used = (sum(_num(p.get("amount_sda"), 0.0) for p in positions.values() if isinstance(p, dict))
            + sum(_num(p.get("position_sda"), 0.0) for p in pending.values() if isinstance(p, dict)))
    count = len(positions) + len(pending)
    return {"open": count, "full": count >= max_open or used + size > capital + 1e-9}


# ---------------------------------------------------------------- SHADOW recorder

def record_scan(candidates, scan_at, *, state_file=STATE_FILE, positions_file=POSITIONS_FILE,
                real_state_file=REAL_STATE_FILE, paper_slots=SLOTS):
    """Persist eligible signals (with lane) and forward quotes of tracked tokens."""
    now = _iso(scan_at) or _now()
    state = _load(state_file, {})
    state.setdefault("schema", 1)
    state.update({"paper_only": True, "real_trading": False, "method": "live_shadow_signal_recorder"})
    state.setdefault("recording_started_at", now.isoformat())
    signals = state.setdefault("signals", {})
    paths = state.setdefault("paths", {})
    tracked = state.setdefault("tracked", {})
    paper_ledger = _load(positions_file, {})
    paper_positions = paper_ledger.get("positions") if isinstance(paper_ledger.get("positions"), dict) else {}
    paper_open = paper_open_count(positions_file)
    real = real_capacity(real_state_file)
    for row in candidates or []:
        token = str(row.get("address") or "").lower()
        price = _num(row.get("price_sda"))
        quote_at = _iso(row.get("quote_at"))
        if not token or not price or price <= 0 or quote_at is None:
            continue
        if row.get("eligible"):
            key = f"{token}|{quote_at.isoformat()}"
            item = signals.get(key)
            if item is None:
                item = signals[key] = {
                    "token": token, "symbol": row.get("symbol"), "quote_at": quote_at.isoformat(),
                    "first_seen": now.isoformat(), "last_seen": now.isoformat(), "price": price,
                    "lane": row.get("paper_entry_lane") or "unknown", "source": "live_recorder",
                    "features": {k: row.get(k) for k in FEATURES}, "sightings": 0,
                    "paper_full_sightings": 0, "real_full_sightings": 0}
            item["last_seen"] = now.isoformat()
            item["sightings"] += 1
            item["paper_full_sightings"] += int(paper_open >= paper_slots)
            item["real_full_sightings"] += int(real["full"])
            opened = paper_positions.get(token)
            if isinstance(opened, dict) and _iso(opened.get("opened_at")) and _iso(opened["opened_at"]) >= quote_at:
                item["paper_opened"] = True
            tracked[token] = now.isoformat()
        if token in tracked:
            series = paths.setdefault(token, [])
            if not series or _iso(series[-1][0]) < quote_at:
                series.append([quote_at.isoformat(), price])
                del series[:-MAX_PATH_POINTS]
    cutoff = now - timedelta(days=TRACK_DAYS)
    for token in [t for t, seen in tracked.items() if (_iso(seen) or now) < cutoff]:
        tracked.pop(token, None)
    if len(signals) > MAX_SIGNALS:
        for key in sorted(signals, key=lambda k: signals[k]["first_seen"])[:len(signals) - MAX_SIGNALS]:
            signals.pop(key)
    state["updated_at"] = now.isoformat()
    state["last_capacity"] = {"paper_open": paper_open, "paper_full": paper_open >= paper_slots,
                              "real_open": real["open"], "real_full": real["full"]}
    _atomic_write(state_file, state)
    return state


# ---------------------------------------------------------------- data loading

def _archive_stream(archive):
    """Historical 30-minute sampled candidate archive (lower fidelity, labelled)."""
    signals, paths = {}, {}
    for o in (archive.get("observations") or []) if isinstance(archive, dict) else []:
        token = str(o.get("address") or "").lower()
        price = _num(o.get("entry_price_sda"))
        quote = _iso(o.get("quote_at"))
        at = _iso(o.get("at"))
        if not token or not price or price <= 0 or quote is None or at is None:
            continue
        paths.setdefault(token, []).append([quote.isoformat(), price])
        for outcome in (o.get("outcomes") or {}).values():
            if isinstance(outcome, dict) and _iso(outcome.get("observed_at")) and _num(outcome.get("price_sda"), 0) > 0:
                paths[token].append([_iso(outcome["observed_at"]).isoformat(), _num(outcome["price_sda"])])
        if o.get("eligible"):
            features = dict(o.get("features") or {})
            features.update({k: o.get(k) for k in ("score", "quality", "impulse") if o.get(k) is not None})
            signals[f"{token}|{quote.isoformat()}"] = {
                "token": token, "symbol": features.get("symbol"), "quote_at": quote.isoformat(),
                "first_seen": at.isoformat(), "last_seen": at.isoformat(), "price": price,
                "lane": o.get("paper_entry_lane") or "unknown", "source": "candidate_archive_30m",
                "features": {k: features.get(k) for k in FEATURES}}
    return signals, paths


def load_stream(state_file=STATE_FILE, archive_file=CANDIDATE_ARCHIVE):
    """Merged chronological signal stream and per-token observed quote paths."""
    signals, paths = _archive_stream(_load(archive_file, {}))
    live = _load(state_file, {})
    signals.update(live.get("signals") or {})
    for token, series in (live.get("paths") or {}).items():
        paths.setdefault(token, []).extend(series)
    clean = {}
    for token, series in paths.items():
        seen = {}
        for stamp, price in series:
            when = _iso(stamp)
            if when is not None and _num(price, 0) > 0:
                seen[when] = float(price)
        clean[token] = sorted(seen.items())
    ordered = sorted(signals.values(), key=lambda s: (s["first_seen"], s["token"]))
    return ordered, clean, live


# ---------------------------------------------------------------- challengers

def veto_metrics(signal):
    f = signal.get("features") or {}
    return {field: _num(f.get(name)) for field, name in VETO_FIELDS.items()}


def strict_thresholds(train_signals):
    """Median of TRAIN-period single-quote signals for each quality/activity key."""
    out = {}
    for key, name in STRICT_KEYS.items():
        values = sorted(v for v in (_num((s.get("features") or {}).get(name))
                                    for s in train_signals if s["lane"] == SINGLE_QUOTE) if v is not None)
        if len(values) >= 4:
            out[key] = round(values[len(values) // 2], 4)
    return out


def lane_challengers(strict):
    def strict_ok(s):
        if s["lane"] != SINGLE_QUOTE:
            return True
        f = s.get("features") or {}
        return all((_num(f.get(STRICT_KEYS[k])) or float("-inf")) >= v for k, v in strict.items())
    return [
        {"id": "A_CURRENT_CHAMPION", "label": "Current Champion (all lanes)", "accept": lambda s: True},
        {"id": "B_CONFIRMED_ONLY", "label": "Exclude swing_single_quote", "accept": lambda s: s["lane"] != SINGLE_QUOTE},
        {"id": "C_SINGLE_QUOTE_ONLY", "label": "swing_single_quote only (research)",
         "accept": lambda s: s["lane"] == SINGLE_QUOTE},
        {"id": "D_STRICT_SINGLE_QUOTE", "label": "swing_single_quote with stricter quality/activity",
         "accept": strict_ok, "thresholds": strict},
    ]


def filter_challenger(candidate_id, buy_filters):
    """Champion-config BUY filter (optimizer search space) as a counterfactual acceptor."""
    import paper_champion_config as champion_config
    return {"id": candidate_id, "label": f"BUY filter {buy_filters}",
            "accept": lambda s: not champion_config.buy_veto(veto_metrics(s), {"buy_filters": buy_filters})}


# ---------------------------------------------------------------- counterfactual replay

def _position(signal, series, fee_rate, slippage):
    from tools import paper_optimizer as po
    quote = _iso(signal["quote_at"])
    observations = [{"at": when.isoformat(), "price": price} for when, price in series if when > quote]
    trade = {"id": f"{signal['token']}|{signal['quote_at']}", "token": signal["token"],
             "opened_at": signal["quote_at"], "fill_price": signal["price"], "investment": INVESTMENT_SDA,
             "observations": observations, "status": "OPEN", "complete": True}
    result = po.replay(trade, None, fee_rate, slippage)
    result["tracked"] = bool(observations)
    return result


def simulate(signals, paths, accept, *, as_of=None, fee_rate=None, slippage=None, slots=SLOTS):
    """Event-driven portfolio replay over the full chronological signal stream."""
    import heapq
    from tools import paper_optimizer as po
    fee_rate = po.FEE_RATE if fee_rate is None else fee_rate
    slippage = po.SLIPPAGE_RATE if slippage is None else slippage
    as_of = as_of or _now()
    stream = sorted((s for s in signals if _iso(s["first_seen"]) and _iso(s["first_seen"]) <= as_of),
                    key=lambda s: (_iso(s["first_seen"]), s["token"], s["quote_at"]))
    clipped = {t: [(w, p) for w, p in series if w <= as_of] for t, series in paths.items()}
    window = timedelta(seconds=SCAN_SECONDS)
    heap = sorted({_iso(s["first_seen"]) for s in stream})
    heapq.heapify(heap)
    processed, pool, idx = set(), [], 0
    open_positions, trades, cooldown = {}, [], {}
    rejected, capacity_blocked, scan_blocked = set(), set(), set()
    accepted, max_open = 0, 0
    while heap:
        t = heapq.heappop(heap)
        if t in processed:
            continue
        processed.add(t)
        for token, pos in list(open_positions.items()):
            if pos["exit_at"] is not None and pos["exit_at"] <= t:
                trades.append(pos["record"])
                cooldown[token] = pos["exit_at"]
                del open_positions[token]
        while idx < len(stream) and _iso(stream[idx]["first_seen"]) <= t:
            pool.append(stream[idx])
            idx += 1
        # A signal stays actionable only while its quote was still being observed as current.
        pool = [s for s in pool if _iso(s["last_seen"]) >= t]
        bought = 0
        for signal in sorted(pool, key=lambda s: (-(_num((s.get("features") or {}).get("score"), 0.0)),
                                                  s["token"], s["quote_at"])):
            key = (signal["token"], signal["quote_at"])
            if not accept(signal):
                rejected.add(key)
                pool.remove(signal)
                continue
            last_exit = cooldown.get(signal["token"])
            if signal["token"] in open_positions or (
                    last_exit is not None and t - last_exit < timedelta(hours=COOLDOWN_HOURS)):
                continue
            if len(open_positions) >= slots:
                capacity_blocked.add(key)
                continue
            if bought >= MAX_BUYS_PER_SCAN:
                scan_blocked.add(key)
                if _iso(signal["last_seen"]) >= t + window:
                    heapq.heappush(heap, t + window)
                continue
            result = _position(signal, clipped.get(signal["token"], []), fee_rate, slippage)
            exit_at = None
            if result["exit_at"] and not result["open"] and result["tracked"] and result["sells"]:
                exit_at = max(_iso(result["exit_at"]), t)
                heapq.heappush(heap, exit_at)
            open_positions[signal["token"]] = {"exit_at": exit_at,
                                               "record": _trade_record(signal, result, t, exit_at, as_of)}
            pool.remove(signal)
            accepted += 1
            bought += 1
            max_open = max(max_open, len(open_positions))
    trades.extend(p["record"] for p in open_positions.values())
    trades.sort(key=lambda r: r["opened_at"])
    return {"trades": trades, "max_open": max_open, "open_at_end": len(open_positions),
            "counters": {"accepted": accepted, "rejected_by_filter": len(rejected),
                         "blocked_capacity": len(capacity_blocked - {(r["token"], r["signal_quote_at"]) for r in trades}),
                         "blocked_per_scan": len(scan_blocked - {(r["token"], r["signal_quote_at"]) for r in trades})}}


def _trade_record(signal, result, opened, exit_at, as_of):
    end = exit_at or as_of
    hours = max(0.0, (end - opened).total_seconds() / 3600)
    f = signal.get("features") or {}
    return {
        "token": signal["token"], "symbol": signal.get("symbol"), "buy_lane": signal["lane"],
        "source": signal.get("source"), "signal_quote_at": signal["quote_at"],
        "opened_at": opened.isoformat(), "entry_price": signal["price"],
        "score": f.get("score"), "quality": f.get("quality"), "impulse": f.get("impulse"),
        "flow_15m": f.get("flow_15m"), "flow_1h": f.get("flow_1h"), "volume": f.get("volume_1h"),
        "trade_count": f.get("trades_1h"), "buy_ratio": f.get("buy_ratio"), "m15": f.get("m15"),
        "m1h": f.get("m1h"), "phase": f.get("phase"),
        "exit_outcome": ("UNTRACKED" if not result["tracked"] else "OPEN" if exit_at is None else "CLOSED"),
        "exit_at": exit_at.isoformat() if exit_at else None,
        "net_pnl": round(result["net"], 6) if result["tracked"] else None,
        "realized_pnl": round(result["realized"], 6), "unrealized_pnl": round(result["unrealized"], 6),
        "mfe": round(result["mfe"], 6), "mae": round(result["mae"], 6), "sells": result["sells"],
        "holding_hours": round(hours, 4), "capital_sda_hours": round(hours * INVESTMENT_SDA, 4),
        "modeled_costs": round(result["fees"], 6), "tracked": result["tracked"],
    }


# ---------------------------------------------------------------- metrics

def lane_metrics(trades):
    tracked = [t for t in trades if t["tracked"]]
    nets = [t["net_pnl"] for t in tracked]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x < 0]
    equity = peak = drawdown = 0.0
    for t in sorted(tracked, key=lambda t: t["exit_at"] or t["opened_at"]):
        equity += t["net_pnl"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    net = sum(nets)
    sda_hours = sum(t["capital_sda_hours"] for t in trades)
    gross_loss = -sum(losses)
    pf = round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None
    n = len(nets)
    return {
        "trades": len(trades), "tracked_trades": n, "untracked_trades": len(trades) - n,
        "tokens": len({t["token"] for t in tracked}),
        "closed_trades": sum(t["exit_outcome"] == "CLOSED" for t in trades),
        "open_trades": sum(t["exit_outcome"] == "OPEN" for t in trades),
        "net_pnl": round(net, 4), "profit_factor": pf,
        "profit_factor_infinite": gross_loss <= 0 and bool(wins),
        "expectancy": round(net / n, 4) if n else None,
        "win_rate": round(len(wins) / n * 100, 2) if n else None,
        "avg_winner": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loser": round(sum(losses) / len(losses), 4) if losses else None,
        "max_drawdown": round(drawdown, 4),
        "avg_mfe_pct": round(sum(t["mfe"] for t in tracked) / n * 100, 3) if n else None,
        "avg_mae_pct": round(sum(t["mae"] for t in tracked) / n * 100, 3) if n else None,
        "avg_holding_hours": round(sum(t["holding_hours"] for t in trades) / len(trades), 3) if trades else None,
        "capital_sda_hours": round(sda_hours, 2),
        "position_hours": round(sum(t["holding_hours"] for t in trades), 2),
        "pnl_per_capital_hour": round(net / sda_hours, 6) if sda_hours else None,
        "pnl_per_100_sda_days": round(net / (sda_hours / 24.0) * 100, 4) if sda_hours else None,
        "best_trade_contribution_pct": round(max(wins) / sum(wins) * 100, 2) if wins else None,
        "modeled_costs": round(sum(t["modeled_costs"] for t in tracked), 4),
    }


def by_lane(trades):
    lanes = sorted({t["buy_lane"] for t in trades})
    return {lane: lane_metrics([t for t in trades if t["buy_lane"] == lane]) for lane in lanes}


# ---------------------------------------------------------------- research run

def _complete_v2_paths(dataset_dir=DATASET_DIR):
    complete = closed = 0
    for path in Path(dataset_dir).glob("*.json"):
        record = _load(path, {})
        if (record.get("data_quality") or {}).get("complete_from_entry"):
            complete += 1
            closed += record.get("status") == "CLOSED"
    return {"complete_from_entry": complete, "complete_and_closed": closed}


def _paper_ledger_lanes(positions_file=POSITIONS_FILE):
    """Observed PAPER ledger by lane (closed rows used retired swing_v1 exits: context only)."""
    ledger = _load(positions_file, {})
    rows = [r for r in ledger.get("closed_trades") or [] if isinstance(r, dict)]
    rows += [p for p in (ledger.get("positions") or {}).values() if isinstance(p, dict)]
    out = {}
    for r in rows:
        temporal = r.get("entry_temporal_recovery") if isinstance(r.get("entry_temporal_recovery"), dict) else {}
        lane = temporal.get("lane") or "unknown"
        item = out.setdefault(lane, {"opened": 0, "closed": 0, "open": 0, "realized_pnl": 0.0, "wins": 0})
        item["opened"] += 1
        if str(r.get("status") or "OPEN").upper() == "CLOSED" or r.get("closed_at"):
            item["closed"] += 1
            pnl = _num(r.get("closed_profit_sda"), 0.0)
            item["realized_pnl"] = round(item["realized_pnl"] + pnl, 4)
            item["wins"] += int(pnl > 0)
        else:
            item["open"] += 1
    return out


def _blocked(signals, live):
    live_signals = [s for s in signals if s.get("source") == "live_recorder"]
    return {
        "basis": "live recorder signals (distinct token+quote); PAPER/REAL capacity read at each sighting",
        "recording_started_at": live.get("recording_started_at"),
        "eligible_signals": len(live_signals),
        "opened_by_paper": sum(bool(s.get("paper_opened")) for s in live_signals),
        "blocked_paper_10_of_10": sum(bool(s.get("paper_full_sightings")) and not s.get("paper_opened")
                                      for s in live_signals),
        "eligible_while_real_10_of_10": sum(bool(s.get("real_full_sightings")) for s in live_signals),
        "last_capacity": live.get("last_capacity"),
        "note": "Counts only. Missed-trade profitability comes solely from the SHADOW replay below.",
    }


def research(now=None, *, state_file=STATE_FILE, archive_file=CANDIDATE_ARCHIVE,
             positions_file=POSITIONS_FILE, dataset_dir=DATASET_DIR):
    """Lane challengers A-D on the full stream with TRAIN/VALIDATION/HOLDOUT/SHADOW gates."""
    from tools import paper_optimizer as po
    now = now or _now()
    signals, paths, live = load_stream(state_file, archive_file)
    v2 = _complete_v2_paths(dataset_dir)
    if not signals:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["no recorded BUY signals"], "decision": "KEEP CHAMPION",
                "dataset_complete_v2_paths": v2, "promotion_applied": False}
    start, end = _iso(signals[0]["first_seen"]), now
    span = end - start
    train_end, val_end = start + span * 0.6, start + span * 0.8
    train_signals = [s for s in signals if _iso(s["first_seen"]) < train_end]
    strict = strict_thresholds(train_signals)
    period = lambda t: ("train" if _iso(t["opened_at"]) < train_end else  # noqa: E731
                        "validation" if _iso(t["opened_at"]) < val_end else "holdout")
    results = {}
    for ch in lane_challengers(strict):
        sim = simulate(signals, paths, ch["accept"], as_of=now)
        trades = sim["trades"]
        cost = {}
        for mult in (1.25, 1.5):
            alt = simulate(signals, paths, ch["accept"], as_of=now, fee_rate=po.FEE_RATE * mult)
            cost[f"net_at_{mult}x_costs"] = lane_metrics(alt["trades"])["net_pnl"]
        overall = lane_metrics(trades)
        results[ch["id"]] = {
            "label": ch["label"], "thresholds": ch.get("thresholds"),
            "thresholds_status": (None if "thresholds" not in ch else "DERIVED_FROM_TRAIN" if strict
                                  else "NOT_DERIVABLE_FROM_TRAIN (fewer than 4 single-quote TRAIN signals; equals Champion)"),
            "overall": overall,
            "cost_sensitivity": cost, "by_lane": by_lane(trades), "counters": sim["counters"],
            "max_open_positions": sim["max_open"],
            "splits": {k: lane_metrics([t for t in trades if period(t) == k])
                       for k in ("train", "validation", "holdout")},
            "shadow": lane_metrics([t for t in trades if t.get("source") == "live_recorder"]),
            "trades": trades[-200:],
        }
    champ = results["A_CURRENT_CHAMPION"]
    gates = {}
    for cid, r in results.items():
        if cid == "A_CURRENT_CHAMPION":
            continue
        gates[cid] = _gates(r, champ, live, now)
    v2_ok = v2["complete_and_closed"] >= po.GATES["min_total_trades"]
    tracked = champ["overall"]["tracked_trades"]
    dq_reasons = []
    if not v2_ok:
        dq_reasons.append(f"only {v2['complete_and_closed']} complete closed V2 paths "
                          f"(need {po.GATES['min_total_trades']})")
    if tracked < po.GATES["min_total_trades"]:
        dq_reasons.append(f"only {tracked} SHADOW-tracked Champion replay trades (need {po.GATES['min_total_trades']})")
    status = "INSUFFICIENT_DATA" if dq_reasons else (
        "RESEARCH_ONLY_ALL_GATES_PASSED" if any(g["all_passed"] for g in gates.values()) else "KEEP_COLLECTING")
    sq = champ["by_lane"].get(SINGLE_QUOTE)
    return {
        "schema": 1, "paper_only": True, "real_trading": False, "generated_at": now.isoformat(),
        "mode": "BUY_LANE_COUNTERFACTUAL_CAPACITY_REPLAY", "status": status, "decision": "KEEP CHAMPION",
        "promotion_applied": False, "promotable": False,
        "reasons": dq_reasons, "data_quality_passed": not dq_reasons,
        "constraints": {"slots": SLOTS, "investment_sda": INVESTMENT_SDA, "capital_sda": SLOTS * INVESTMENT_SDA,
                        "max_buys_per_scan": MAX_BUYS_PER_SCAN, "cooldown_hours": COOLDOWN_HOURS,
                        "sell_policy": "swing_sell_v2.decide", "fee_rate": po.FEE_RATE,
                        "slippage_rate": po.SLIPPAGE_RATE},
        "stream": {"signals": len(signals),
                   "live_recorder_signals": sum(s.get("source") == "live_recorder" for s in signals),
                   "archive_signals": sum(s.get("source") == "candidate_archive_30m" for s in signals),
                   "from": signals[0]["first_seen"], "to": signals[-1]["first_seen"],
                   "tokens_with_paths": len(paths), "train_end": train_end.isoformat(),
                   "validation_end": val_end.isoformat()},
        "challengers": results, "gates": gates,
        "single_quote_assessment": _assess(sq, champ, results, dq_reasons),
        "blocked_opportunities": _blocked(signals, live),
        "paper_ledger_by_lane": _paper_ledger_lanes(positions_file),
        "dataset_complete_v2_paths": v2,
    }


def _gates(r, champ, live, now):
    from tools import paper_optimizer as po
    out = {}
    def adapt(m):
        return {"trades": m["tracked_trades"], "tokens": m["tokens"], "net_pnl": m["net_pnl"],
                "profit_factor": m["profit_factor"] or 0.0,
                "profit_factor_infinite": m["profit_factor_infinite"],
                "expectancy": m["expectancy"] or 0.0, "max_drawdown": m["max_drawdown"],
                "single_trade_share": (m["best_trade_contribution_pct"] or 100.0) / 100.0}
    for split, gate in (("train", po.gate_train), ("validation", po.gate_validation), ("holdout", po.gate_holdout)):
        ok, reasons = gate(adapt(r["splits"][split]), adapt(champ["splits"][split]))
        out[split] = {"passed": ok, "reasons": reasons}
    started = _iso(live.get("recording_started_at"))
    days = (now - started).total_seconds() / 86400 if started else 0.0
    shadow_reasons = []
    if r["shadow"]["closed_trades"] < po.GATES["shadow_min_trades"]:
        shadow_reasons.append(f"only {r['shadow']['closed_trades']} closed SHADOW trades "
                              f"(need {po.GATES['shadow_min_trades']})")
    if days < po.GATES["shadow_min_days"]:
        shadow_reasons.append(f"SHADOW recording {days:.1f} days (need {po.GATES['shadow_min_days']})")
    if r["shadow"]["net_pnl"] <= champ["shadow"]["net_pnl"] + po.GATES["min_improvement_sda"]:
        shadow_reasons.append("SHADOW PnL not above Champion over the same stream")
    out["shadow"] = {"passed": not shadow_reasons, "reasons": shadow_reasons, "days": round(days, 2)}
    out["all_passed"] = all(v["passed"] for k, v in out.items() if isinstance(v, dict))
    return out


def _assess(sq, champ, results, dq_reasons):
    b = results.get("B_CONFIRMED_ONLY", {}).get("overall", {})
    a = champ["overall"]
    detail = {"champion_expectancy": a.get("expectancy"), "confirmed_only_expectancy": b.get("expectancy"),
              "champion_net": a.get("net_pnl"), "confirmed_only_net": b.get("net_pnl"),
              "single_quote_lane": sq,
              "single_quote_share_of_capital_hours": (
                  round(sq["capital_sda_hours"] / a["capital_sda_hours"] * 100, 2)
                  if sq and a.get("capital_sda_hours") else None)}
    if dq_reasons or not sq or (sq.get("tracked_trades") or 0) < 10:
        return {"verdict": "INSUFFICIENT DATA", **detail}
    profitable = (sq.get("expectancy") or 0) > 0 and (a.get("net_pnl") or 0) >= (b.get("net_pnl") or 0)
    return {"verdict": "PROFITABLE" if profitable else "UNPROFITABLE", **detail}


def counterfactual_gate(buy_filters, now=None, **paths):
    """Existing BUY filter challengers must also beat the Champion with capacity effects."""
    report_now = now or _now()
    signals, stream_paths, _live = load_stream(paths.get("state_file", STATE_FILE),
                                               paths.get("archive_file", CANDIDATE_ARCHIVE))
    if not signals:
        return False, ["no recorded signal stream for counterfactual capacity replay"]
    champ = lane_metrics(simulate(signals, stream_paths, lambda s: True, as_of=report_now)["trades"])
    chall = lane_metrics(simulate(signals, stream_paths, filter_challenger("buy", buy_filters)["accept"],
                                  as_of=report_now)["trades"])
    reasons = []
    if chall["tracked_trades"] < 10:
        reasons.append(f"only {chall['tracked_trades']} counterfactual tracked trades")
    if (chall["net_pnl"] or 0) <= (champ["net_pnl"] or 0):
        reasons.append("counterfactual capacity replay net PnL not above Champion")
    return not reasons, reasons


# ---------------------------------------------------------------- reporting

def _fmt(value, digits=2):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def render_text(lane_report):
    if not lane_report or not lane_report.get("challengers"):
        status = (lane_report or {}).get("status") or "INSUFFICIENT_DATA"
        detail = (lane_report or {}).get("error") or "collecting signals"
        return f"📊 BUY LANE RESEARCH\n\n{status} — {detail}."
    champ = lane_report["challengers"]["A_CURRENT_CHAMPION"]
    lines = ["📊 BUY LANE RESEARCH", f"Status: {lane_report['status']} • Decision: {lane_report['decision']}", "",
             f"{'Lane':<24}{'Trades':>7}{'PnL':>9}{'PF':>7}"]
    for lane, m in champ["by_lane"].items():
        lines.append(f"{lane[:23]:<24}{m['trades']:>7}{_fmt(m['net_pnl']):>9}{_fmt(m['profit_factor']):>7}")
    lines += ["", "Challengers (same 10 slots, 20 SDA, Swing SELL V2, costs)",
              f"{'Challenger':<24}{'Trades':>7}{'PnL':>9}{'PF':>7}{'PnL/100SDA-d':>14}"]
    for cid, r in lane_report["challengers"].items():
        m = r["overall"]
        lines.append(f"{cid[:23]:<24}{m['trades']:>7}{_fmt(m['net_pnl']):>9}{_fmt(m['profit_factor']):>7}"
                     f"{_fmt(m['pnl_per_100_sda_days']):>14}")
    assessment = lane_report.get("single_quote_assessment") or {}
    blocked = lane_report.get("blocked_opportunities") or {}
    lines += ["", f"swing_single_quote: {assessment.get('verdict')}",
              f"Blocked (PAPER 10/10): {blocked.get('blocked_paper_10_of_10')} • "
              f"eligible while REAL 10/10: {blocked.get('eligible_while_real_10_of_10')}",
              f"Complete V2 paths: {(lane_report.get('dataset_complete_v2_paths') or {}).get('complete_and_closed')}"]
    lines += [f"• {reason}" for reason in lane_report.get("reasons") or []]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    report = research()
    if "--json" in argv:
        print(json.dumps({k: v for k, v in report.items() if k != "challengers"}, indent=2, default=str))
    print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
