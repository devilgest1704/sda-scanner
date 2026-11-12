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

import hashlib
import json
import os
import re
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
    open_positions = {k: p for k, p in positions.items() if isinstance(p, dict)
                      and str(p.get("status") or "OPEN").upper() not in {"CLOSED", "CANCELLED"}}
    active_pending = {k: p for k, p in pending.items() if isinstance(p, dict)}
    used = (sum(_num(p.get("amount_sda", p.get("investment_sda")), 0.0)
            for p in open_positions.values())
            + sum(_num(p.get("position_sda", p.get("amount_sda")), 0.0)
              for p in active_pending.values()))
    count = len(open_positions) + len(active_pending)
    return {"open": count, "capital_used_sda": round(used, 6), "max_open": max_open,
            "capital_sda": capital, "position_sda": size,
            "full": count >= max_open or used + size > capital + 1e-9,
            "held_tokens": sorted(str(k).lower() for k in open_positions),
            "pending_tokens": sorted(str(k).lower() for k in active_pending)}


# ---------------------------------------------------------------- SHADOW recorder

def record_scan(candidates, scan_at, *, state_file=STATE_FILE, positions_file=POSITIONS_FILE,
                real_state_file=REAL_STATE_FILE, paper_slots=SLOTS):
    """Persist eligible signals (with lane) and forward quotes of tracked tokens."""
    now = _iso(scan_at) or _now()
    state = _load(state_file, {})
    state.setdefault("schema", 1)
    state.update({"paper_only": True, "real_trading": False,
                  "method": "live_shadow_signal_recorder",
                  "architecture": "shared_buy_signal_independent_paper_real_capacity",
                  "shadow_real_admission": "ACTIVE"})
    state.setdefault("recording_started_at", now.isoformat())
    signals = state.setdefault("signals", {})
    observations = state.setdefault("observations", {})
    processed = state.setdefault("processed_signal_ids", {})
    admissions = state.setdefault("shadow_real_admissions", [])
    shadow_buys = state.setdefault("shadow_real_buys", [])
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
            canonical = row.get("buy_signal") if isinstance(row.get("buy_signal"), dict) else {}
            strategy_version = str(canonical.get("strategy_version") or row.get("strategy_version") or "V30")
            champion_version = str(canonical.get("champion_version") or row.get("champion_version") or "unknown")
            key = (canonical.get("signal_id") or hashlib.sha256(
                f"{token}|{quote_at.isoformat()}|{strategy_version}|{champion_version}".encode()).hexdigest())
            item = signals.get(key)
            if item is None:
                item = signals[key] = {
                    **canonical, "signal_id": key, "token": token,
                    "contract": canonical.get("contract") or token,
                    "symbol": row.get("symbol"), "quote_at": quote_at.isoformat(),
                    "timestamp": canonical.get("timestamp") or now.isoformat(),
                    "buy_signal": canonical or None,
                    "buy_lane": canonical.get("buy_lane") or row.get("paper_entry_lane") or "unknown",
                    "first_seen": now.isoformat(), "last_seen": now.isoformat(), "price": price,
                    "lane": canonical.get("buy_lane") or row.get("paper_entry_lane") or "unknown",
                    "source": "live_recorder",
                    "features": canonical.get("features") or {k: row.get(k) for k in FEATURES},
                    "freshness": {**(canonical.get("freshness") or {}),
                                  "quote_age_seconds": max(0.0, (now - quote_at).total_seconds()),
                                  "data_age_seconds": _num(row.get("data_age_sec"),
                                                            _num((canonical.get("freshness") or {}).get("data_age_sec"))),
                                  "max_age_seconds": MAX_SIGNAL_AGE_SECONDS},
                    "fee_tier": canonical.get("fee_tier", row.get("fee_tier")),
                    "liquidity_evidence": canonical.get("liquidity_evidence") or row.get("liquidity_evidence"),
                    "decision_reason": canonical.get("decision_reason") or row.get("decision_reason") or "READY",
                    "strategy_version": strategy_version,
                    "champion_version": champion_version,
                    "metadata_valid": row.get("metadata_valid"), "sightings": 0,
                    "paper_full_sightings": 0, "real_full_sightings": 0,
                    "duplicate_count": 0, "duplicate_result": "FIRST_SEEN"}
            item["last_seen"] = now.isoformat()
            item["sightings"] += 1
            item["paper_full_sightings"] += int(paper_open >= paper_slots)
            item["real_full_sightings"] += int(real["full"])
            item["paper_open_at_signal"] = paper_open
            item["real_open_at_signal"] = real["open"]
            item["real_capital_used_sda"] = real["capital_used_sda"]
            opened = paper_positions.get(token)
            if isinstance(opened, dict) and _iso(opened.get("opened_at")) and _iso(opened["opened_at"]) >= quote_at:
                item["paper_opened"] = True
            tracked[token] = now.isoformat()
            if key not in processed:
                decision = _shadow_real_admission(item, row, now, paper_open, paper_slots, real)
                processed[key] = decision
                admissions.append(decision)
                if decision["status"] == "SHADOW_REAL_BUY":
                    shadow_buys.append(decision)
            else:
                item["duplicate_count"] += 1
                item["duplicate_result"] = "DUPLICATE_SIGNAL_SKIPPED"
                processed[key]["duplicate_count"] = item["duplicate_count"]
                processed[key]["duplicate_result"] = item["duplicate_result"]
                processed[key]["last_duplicate_at"] = now.isoformat()
                for admission in reversed(admissions):
                    if admission.get("signal_id") == key:
                        admission["duplicate_count"] = item["duplicate_count"]
                        admission["duplicate_result"] = item["duplicate_result"]
                        admission["last_duplicate_at"] = now.isoformat()
                        break
        if token in tracked:
            series = paths.setdefault(token, [])
            if not series or _iso(series[-1][0]) < quote_at:
                series.append([quote_at.isoformat(), price])
                del series[:-MAX_PATH_POINTS]
                evidence = observations.setdefault(token, [])
                evidence.append([quote_at.isoformat(), price, now.isoformat(), _num(row.get("data_age_sec"))])
                del evidence[:-MAX_PATH_POINTS]
    cutoff = now - timedelta(days=TRACK_DAYS)
    for token in [t for t, seen in tracked.items() if (_iso(seen) or now) < cutoff]:
        tracked.pop(token, None)
    if len(signals) > MAX_SIGNALS:
        for key in sorted(signals, key=lambda k: signals[k]["first_seen"])[:len(signals) - MAX_SIGNALS]:
            signals.pop(key)
    state["shadow_real_admissions"] = admissions[-MAX_SIGNALS:]
    state["shadow_real_buys"] = shadow_buys[-MAX_SIGNALS:]
    state["updated_at"] = now.isoformat()
    state["last_capacity"] = {"paper_open": paper_open, "paper_full": paper_open >= paper_slots,
                              "real_open": real["open"], "real_full": real["full"]}
    _atomic_write(state_file, state)
    return state


MAX_SIGNAL_AGE_SECONDS = 15 * 60


def _shadow_real_admission(signal, row, now, paper_open, paper_slots, real):
    """Make one durable, research-only REAL admission decision per shared signal."""
    quote_at = _iso(signal.get("quote_at"))
    age = (now - quote_at).total_seconds() if quote_at else None
    freshness = signal.get("freshness") or {}
    data_age = _num(freshness.get("data_age_seconds", freshness.get("data_age_sec")))
    is_fresh = (age is not None and -60 <= age <= MAX_SIGNAL_AGE_SECONDS
                and data_age is not None and 0 <= data_age <= MAX_SIGNAL_AGE_SECONDS)
    contract = str(signal.get("contract") or "")
    token = str(signal.get("token") or "").lower()
    status, reason = "SHADOW_REAL_BUY", "REAL_CAPACITY_AVAILABLE"
    paper_admission = "PAPER_CAPACITY_BLOCKED" if paper_open >= paper_slots else "PAPER_ADMISSION_ELIGIBLE"
    if (age is None or age < -60 or age > MAX_SIGNAL_AGE_SECONDS or data_age is None
            or data_age < 0 or data_age > MAX_SIGNAL_AGE_SECONDS):
        status, reason = "STALE_SIGNAL_SKIPPED", "signal exceeds 15-minute freshness limit"
    elif not re.fullmatch(r"0x[a-fA-F0-9]{40}", contract) or row.get("metadata_valid") is False:
        status, reason = "INVALID_METADATA_SKIPPED", "contract or token metadata is invalid"
    elif token in real["held_tokens"]:
        status, reason = "REAL_ALREADY_HELD", "token already has a REAL position"
    elif token in real["pending_tokens"]:
        status, reason = "REAL_BUY_PENDING", "token already has a pending REAL BUY"
    elif real["full"]:
        status, reason = "REAL_CAPACITY_BLOCKED", "REAL slots or capital are full"
    elif paper_open >= paper_slots:
        reason = "PAPER_CAPACITY_BLOCKED_REAL_INDEPENDENT"
    return {"action": status, "status": status, "signal_id": signal.get("signal_id"),
            "token": token, "timestamp": now.isoformat(),
            "signal": signal.get("buy_signal") or signal,
            "paper_slots": {"open": paper_open, "max": paper_slots},
            "real_slots": {"open": real["open"], "max": real["max_open"]},
            "real_capital": {"used_sda": real["capital_used_sda"], "max_sda": real["capital_sda"],
                             "buy_size_sda": real["position_sda"]},
            "paper_admission": paper_admission,
            "freshness_result": "FRESH" if is_fresh else "STALE_OR_UNKNOWN",
            "duplicate_result": "FIRST_SEEN", "duplicate_count": 0,
            "reason": reason, "paper_full": paper_open >= paper_slots,
            "real_full": real["full"], "execution": "SHADOW_ONLY"}


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
    for token, series in (live.get("observations") or {}).items():
        paths.setdefault(token, []).extend(series)
    clean = {}
    for token, series in paths.items():
        seen = {}
        for point in series:
            if isinstance(point, dict):
                stamp, price = point.get("at"), point.get("price")
                observed_at, data_age = point.get("observed_at"), point.get("data_age_sec")
            else:
                stamp, price = point[0], point[1]
                observed_at = point[2] if len(point) > 2 else None
                data_age = point[3] if len(point) > 3 else None
            when = _iso(stamp)
            if when is not None and _num(price, 0) > 0:
                seen[when] = (float(price), _iso(observed_at), _num(data_age))
        clean[token] = [(when, *values) for when, values in sorted(seen.items())]
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

def _position(signal, series, fee_rate=None, slippage=None, investment_sda=INVESTMENT_SDA,
              stale_variant=None, signals=(), paths=None, as_of=None):
    from tools import paper_optimizer as po
    fee_rate = po.FEE_RATE if fee_rate is None else fee_rate
    slippage = po.SLIPPAGE_RATE if slippage is None else slippage
    quote = _iso(signal["quote_at"])
    observations = [{"at": point[0].isoformat(), "price": point[1],
                     "observed_at": point[2].isoformat() if len(point) > 2 and point[2] else None,
                     "data_age_sec": point[3] if len(point) > 3 else None}
                    for point in series if point[0] > quote]
    trade = {"id": signal.get("signal_id") or f"{signal['token']}|{signal['quote_at']}", "token": signal["token"],
             "opened_at": signal.get("first_seen") or signal["quote_at"],
             "fill_price": signal["price"], "investment": investment_sda,
             "observations": observations, "status": "OPEN", "complete": True}
    result = po.replay(trade, None, fee_rate, slippage)
    if stale_variant:
        overlay = _stale_exit(signal, series, **STALE_PROFIT_VARIANTS[stale_variant],
                              signals=signals, paths=paths or {}, as_of=as_of or _now(),
                              fee_rate=fee_rate, slippage=slippage, investment_sda=investment_sda)
        policy_exit = _iso(result.get("exit_at"))
        if overlay and (result.get("open") or policy_exit is None or overlay["at"] < policy_exit):
            result.update({"net": overlay["net"], "realized": overlay["net"], "unrealized": 0.0,
                           "exit_at": overlay["at"].isoformat(), "open": False, "censored": False,
                           "sells": 1, "fees": overlay["fees"], "holding_hours": overlay["holding_hours"],
                           "stale_profit_exit": True,
                           "replacement_signal_id": ((overlay.get("replacement") or {}).get("signal_id")
                                                     or (f"{overlay['replacement'].get('token')}|"
                                                         f"{overlay['replacement'].get('quote_at')}"
                                                         if overlay.get("replacement") else None)),
                           "replacement_token": (overlay.get("replacement") or {}).get("token"),
                           "replacement_net": overlay.get("replacement_net")})
        else:
            result["stale_profit_exit"] = False
    else:
        result["stale_profit_exit"] = False
    result["tracked"] = bool(observations)
    return result


def simulate(signals, paths, accept, *, as_of=None, fee_rate=None, slippage=None, slots=SLOTS,
             capital_sda=200.0, investment_sda=INVESTMENT_SDA, require_fresh_signals=False,
             stale_variant=None, one_shot_signals=False):
    """Event-driven portfolio replay over the full chronological signal stream."""
    import heapq
    from tools import paper_optimizer as po
    fee_rate = po.FEE_RATE if fee_rate is None else fee_rate
    slippage = po.SLIPPAGE_RATE if slippage is None else slippage
    as_of = as_of or _now()
    stream = sorted((s for s in signals if _iso(s["first_seen"]) and _iso(s["first_seen"]) <= as_of
                     and (not require_fresh_signals or _fresh_signal(s))),
                    key=lambda s: (_iso(s["first_seen"]), s["token"], s["quote_at"]))
    clipped = {t: [point for point in series if point[0] <= as_of] for t, series in paths.items()}
    window = timedelta(seconds=SCAN_SECONDS)
    heap = sorted({_iso(s["first_seen"]) for s in stream})
    heapq.heapify(heap)
    processed, pool, idx = set(), [], 0
    open_positions, trades, cooldown = {}, [], {}
    rejected, capacity_blocked, scan_blocked = set(), set(), set()
    accepted, max_open, max_capital_used = 0, 0, 0.0
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
                if one_shot_signals:
                    pool.remove(signal)
                continue
            if (len(open_positions) >= slots
                    or (len(open_positions) + 1) * investment_sda > capital_sda + 1e-9):
                capacity_blocked.add(key)
                if one_shot_signals:
                    pool.remove(signal)
                continue
            if bought >= MAX_BUYS_PER_SCAN:
                scan_blocked.add(key)
                if one_shot_signals:
                    pool.remove(signal)
                elif _iso(signal["last_seen"]) >= t + window:
                    heapq.heappush(heap, t + window)
                continue
            result = _position(signal, clipped.get(signal["token"], []), fee_rate, slippage, investment_sda,
                               stale_variant=stale_variant, signals=stream, paths=clipped, as_of=as_of)
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
            max_capital_used = max(max_capital_used, len(open_positions) * investment_sda)
    trades.extend(p["record"] for p in open_positions.values())
    trades.sort(key=lambda r: r["opened_at"])
    return {"trades": trades, "max_open": max_open, "open_at_end": len(open_positions),
            "max_capital_used_sda": round(max_capital_used, 4),
            "counters": {"accepted": accepted, "rejected_by_filter": len(rejected),
                         "blocked_capacity": len(capacity_blocked - {(r["token"], r["signal_quote_at"]) for r in trades}),
                         "blocked_per_scan": len(scan_blocked - {(r["token"], r["signal_quote_at"]) for r in trades})}}


def _trade_record(signal, result, opened, exit_at, as_of):
    end = exit_at or as_of
    hours = max(0.0, (end - opened).total_seconds() / 3600)
    f = signal.get("features") or {}
    return {
        "signal_id": signal.get("signal_id"), "token": signal["token"],
        "symbol": signal.get("symbol"), "buy_lane": signal.get("buy_lane", signal["lane"]),
        "source": signal.get("source"), "signal_quote_at": signal["quote_at"],
        "opened_at": opened.isoformat(), "entry_price": signal["price"],
        "score": f.get("score"), "quality": f.get("quality"), "impulse": f.get("impulse"),
        "flow_15m": f.get("flow_15m"), "flow_1h": f.get("flow_1h"), "volume": f.get("volume_1h"),
        "trade_count": f.get("trades_1h"), "buy_ratio": f.get("buy_ratio"), "m15": f.get("m15"),
        "m1h": f.get("m1h"), "phase": f.get("phase"),
        "exit_outcome": ("UNTRACKED" if not result["tracked"] else "OPEN" if exit_at is None else "CLOSED"),
        "exit_at": exit_at.isoformat() if exit_at else None,
        "observed_until": as_of.isoformat(),
        "net_pnl": round(result["net"], 6) if result["tracked"] else None,
        "realized_pnl": round(result["realized"], 6), "unrealized_pnl": round(result["unrealized"], 6),
        "mfe": round(result["mfe"], 6), "mae": round(result["mae"], 6), "sells": result["sells"],
        "holding_hours": round(hours, 4),
        "capital_sda_hours": round(hours * result.get("investment", INVESTMENT_SDA), 4),
        "modeled_costs": round(result["fees"], 6), "tracked": result["tracked"],
        "stale_profit_exit": bool(result.get("stale_profit_exit")),
        "replacement_signal_id": result.get("replacement_signal_id"),
        "replacement_token": result.get("replacement_token"),
        "replacement_net": result.get("replacement_net"),
    }


def simulate_independent_capacity(signals, paths, accept, *, as_of=None, paper_slots=SLOTS,
                                  real_slots=SLOTS, capital_sda=200.0,
                                  investment_sda=INVESTMENT_SDA):
    """Replay the same fresh signal stream through independent PAPER and REAL ledgers."""
    paper = simulate(signals, paths, accept, as_of=as_of, slots=paper_slots,
                     capital_sda=capital_sda, investment_sda=investment_sda,
                     require_fresh_signals=True, one_shot_signals=True)
    real = simulate(signals, paths, accept, as_of=as_of, slots=real_slots,
                    capital_sda=capital_sda, investment_sda=investment_sda,
                    require_fresh_signals=True, one_shot_signals=True)
    paper_ids = {t.get("signal_id") or f"{t['token']}|{t['signal_quote_at']}" for t in paper["trades"]}
    real_ids = {t.get("signal_id") or f"{t['token']}|{t['signal_quote_at']}" for t in real["trades"]}
    paper_signals = [s for s in signals if (s.get("signal_id") or f"{s['token']}|{s['quote_at']}") in paper_ids]
    architecture_a = simulate(paper_signals, paths, accept, as_of=as_of, slots=real_slots,
                              capital_sda=capital_sda, investment_sda=investment_sda,
                              require_fresh_signals=True, one_shot_signals=True)
    architecture_a_ids = {t.get("signal_id") or f"{t['token']}|{t['signal_quote_at']}"
                          for t in architecture_a["trades"]}
    return {"paper": paper, "real": real,
            "architecture_a_real": architecture_a,
            "architecture_b_real": real,
            "architecture_a_only": sorted(architecture_a_ids - real_ids),
            "architecture_b_only": sorted(real_ids - architecture_a_ids),
            "limits": {"paper_slots": paper_slots, "real_slots": real_slots,
                       "capital_sda": capital_sda, "investment_sda": investment_sda}}


def _fresh_signal(signal):
    first_seen, quote_at = _iso(signal.get("first_seen")), _iso(signal.get("quote_at"))
    if first_seen is None or quote_at is None:
        return False
    age = (first_seen - quote_at).total_seconds()
    data_age = _num((signal.get("freshness") or {}).get("data_age_seconds"))
    return 0 <= age <= MAX_SIGNAL_AGE_SECONDS and (data_age is None or 0 <= data_age <= MAX_SIGNAL_AGE_SECONDS)


STALE_PROFIT_VARIANTS = {
    "T1": {"age_hours": 48, "min_profit_pct": 3, "replacement_required": False},
    "T2": {"age_hours": 72, "min_profit_pct": 3, "replacement_required": False},
    "T3": {"age_hours": 48, "min_profit_pct": 5, "replacement_required": False},
    "T4": {"age_hours": 48, "min_profit_pct": 3, "replacement_required": True},
}


def _replacement_waiting(held, when, signals):
    held_token = str(held.get("token") or "").lower()
    for candidate in signals:
        if str(candidate.get("token") or "").lower() == held_token:
            continue
        first_seen, last_seen, quote_at = (_iso(candidate.get("first_seen")),
                                            _iso(candidate.get("last_seen")),
                                            _iso(candidate.get("quote_at")))
        features = candidate.get("features") or {}
        freshness = candidate.get("freshness") or {}
        score = _num(features.get("score"), 0)
        quality = _num(features.get("quality"), 0)
        data_age = _num(freshness.get("data_age_seconds"), float("inf"))
        paper_was_full = _num(candidate.get("paper_open_at_signal"), 0) >= SLOTS
        if (first_seen and last_seen and quote_at and first_seen <= when <= last_seen + timedelta(seconds=60)
                and 0 <= (when - quote_at).total_seconds() <= 300 and data_age <= 300
            and paper_was_full and score >= 75 and quality >= 65):
            return candidate
    return None


def _stale_exit(signal, series, *, age_hours, min_profit_pct, replacement_required,
                signals, paths, as_of, fee_rate, slippage, investment_sda):
    from tools import paper_optimizer as po
    opened = _iso(signal.get("first_seen")) or _iso(signal.get("quote_at"))
    quote_at = _iso(signal.get("quote_at"))
    entry = _num(signal.get("price"), 0)
    if opened is None or quote_at is None or entry <= 0:
        return None
    state = {"profit_partial_done": False, "trailing_armed": False,
             "highest_price_since_trailing_activation": None}
    peak, peak_at = entry, opened
    for point in series:
        when, price = point[0], _num(point[1], 0)
        observed_at = point[2] if len(point) > 2 else None
        data_age = _num(point[3]) if len(point) > 3 else None
        if when <= quote_at or when > as_of or not observed_at or data_age is None or not 0 <= data_age <= MAX_SIGNAL_AGE_SECONDS:
            continue
        pnl = price / (entry * (1 + fee_rate)) - 1
        position = {"profit_partial_done": state["profit_partial_done"],
                    "trailing_armed": state["trailing_armed"],
                    "highest_price_since_trailing_activation": state["highest_price_since_trailing_activation"]}
        decision = po.policy.decide(position, pnl, price)
        if decision["action"] == "FULL_SELL":
            return None
        if decision["action"] == "PARTIAL_SELL":
            state["profit_partial_done"] = True
            state["trailing_armed"] = True
            state["highest_price_since_trailing_activation"] = price
        if price > peak * 1.01:
            peak, peak_at = price, observed_at
        age = (observed_at - opened).total_seconds() / 3600
        stale = (age >= age_hours and pnl * 100 >= min_profit_pct
                 and (observed_at - peak_at).total_seconds() >= 24 * 3600
                 and not state["trailing_armed"] and decision["action"] == "HOLD")
        if stale:
            replacement = _replacement_waiting(signal, observed_at, signals) if replacement_required else None
            if replacement_required and replacement is None:
                continue
            proceeds = investment_sda * price / entry * (1 - slippage)
            fees = investment_sda * fee_rate + proceeds * fee_rate
            net = proceeds * (1 - fee_rate) - investment_sda * (1 + fee_rate)
            replacement_net = None
            if replacement is not None:
                replacement_series = [p for p in paths.get(replacement.get("token"), []) if p[0] <= as_of]
                repl_trade = _position(replacement, replacement_series, fee_rate, slippage, investment_sda)
                replacement_net = repl_trade["net"] if repl_trade["tracked"] else None
            return {"at": observed_at, "price": price, "net": net, "fees": fees,
                    "holding_hours": max(0.0, age), "replacement": replacement,
                    "replacement_net": replacement_net}
    return None


def stale_profit_research(signals, paths, *, as_of=None):
    """Shadow-only comparison of Swing SELL V2 (T0) with stale-profit overlays T1-T4."""
    from tools import paper_optimizer as po
    as_of = as_of or _now()
    baseline = simulate(signals, paths, lambda _signal: True, as_of=as_of,
                        fee_rate=po.FEE_RATE, slippage=po.SLIPPAGE_RATE,
                        require_fresh_signals=True, one_shot_signals=True)
    replays = {"T0": baseline}
    for variant in STALE_PROFIT_VARIANTS:
        replays[variant] = simulate(signals, paths, lambda _signal: True, as_of=as_of,
                                    fee_rate=po.FEE_RATE, slippage=po.SLIPPAGE_RATE,
                                    require_fresh_signals=True, stale_variant=variant,
                                    one_shot_signals=True)
    base_by_id = {t.get("signal_id") or f"{t['token']}|{t['signal_quote_at']}": t
                  for t in baseline["trades"]}
    variants, recycling_report = {}, {}
    for name, replay in replays.items():
        trades = replay["trades"]
        metrics = lane_metrics(trades)
        variants[name] = {
            "tracked_positions": metrics["tracked_trades"],
            "net_pnl_after_costs": metrics["net_pnl"],
            "profit_factor": metrics["profit_factor"] if metrics["profit_factor"] is not None else
                             ("infinite" if metrics["profit_factor_infinite"] else None),
            "expectancy": metrics["expectancy"], "max_drawdown_sda": metrics["max_drawdown"],
            "average_holding_hours": metrics["avg_holding_hours"],
            "pnl_per_100_sda_days": metrics["pnl_per_100_sda_days"],
            "capital_sda_hours": metrics["capital_sda_hours"],
            "capital_utilization_pct": metrics["capital_utilization_pct"],
            "stale_profit_exits": sum(t.get("stale_profit_exit", False) for t in trades),
            "modeled_costs": metrics["modeled_costs"], "max_open_positions": replay["max_open"],
            "max_capital_used_sda": replay["max_capital_used_sda"],
        }
        if name == "T0":
            continue
        stale = [t for t in trades if t.get("stale_profit_exit")]
        released = regret = extra_costs = replacement_benefit = 0.0
        replacements = set()
        for trade in stale:
            key = trade.get("signal_id") or f"{trade['token']}|{trade['signal_quote_at']}"
            old = base_by_id.get(key)
            if old:
                regret += (old.get("net_pnl") or 0) - (trade.get("net_pnl") or 0)
                released += max(0, old.get("capital_sda_hours", 0) - trade.get("capital_sda_hours", 0))
                extra_costs += trade.get("modeled_costs", 0) - old.get("modeled_costs", 0)
            replacement_id = trade.get("replacement_signal_id")
            if replacement_id:
                replacements.add(replacement_id)
                replacement_trade = next((candidate for candidate in trades
                                          if (candidate.get("signal_id") or
                                              f"{candidate['token']}|{candidate['signal_quote_at']}") == replacement_id), None)
                if replacement_trade:
                    replacement_benefit += replacement_trade.get("net_pnl") or 0
        recycling_report[name] = {
            "stale_profit_exits": len(stale), "replacement_opportunities": len(replacements),
            "replacement_candidates": len(replacements),
            "replacement_net_benefit_sda": round(replacement_benefit, 6),
            "regret_held_vs_exit_sda": round(regret, 6),
            "capital_hours_released": round(released, 4),
            "incremental_costs_sda": round(extra_costs, 6),
        }
    return {"active": True, "scope": "PAPER_SHADOW_ONLY", "production_real_time_exit": False,
            "method": "event-driven portfolio replay; Swing SELL V2 baseline; fresh signals; 10 slots and 200 SDA",
            "status": "INSUFFICIENT_DATA" if variants["T0"]["tracked_positions"] < 20 else "COLLECTING",
            "constraints": {"slots": SLOTS, "investment_sda": INVESTMENT_SDA,
                            "capital_sda": SLOTS * INVESTMENT_SDA,
                            "sell_policy": "swing_sell_v2.decide",
                            "fee_rate": po.FEE_RATE, "slippage_rate": po.SLIPPAGE_RATE},
            "variants": variants,
            "capital_recycling": {"by_variant": recycling_report}}


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
    starts = [_iso(t.get("opened_at")) for t in trades if _iso(t.get("opened_at"))]
    ends = [_iso(t.get("exit_at") or t.get("observed_until") or t.get("opened_at"))
            for t in trades if _iso(t.get("exit_at") or t.get("observed_until") or t.get("opened_at"))]
    observed_hours = max(0.0, (max(ends) - min(starts)).total_seconds() / 3600) if starts and ends else 0.0
    capacity_hours = observed_hours * SLOTS * INVESTMENT_SDA
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
        "capital_utilization_pct": round(sda_hours / capacity_hours * 100, 2) if capacity_hours else None,
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
    decisions = live.get("shadow_real_admissions") or []
    shadow_buys = [a for a in decisions if a.get("status") == "SHADOW_REAL_BUY"]
    paper_full_real_free = [a for a in shadow_buys if a.get("paper_full")]
    return {
        "basis": "live recorder signals (distinct token+quote); PAPER/REAL capacity read at each sighting",
        "recording_started_at": live.get("recording_started_at"),
        "eligible_signals": len(live_signals),
        "opened_by_paper": sum(bool(s.get("paper_opened")) for s in live_signals),
        "blocked_paper_10_of_10": sum(bool(s.get("paper_full_sightings")) and not s.get("paper_opened")
                                      for s in live_signals),
        "eligible_while_real_10_of_10": sum(bool(s.get("real_full_sightings")) for s in live_signals),
        "signals_blocked_by_paper_capacity": sum(bool(s.get("paper_full_sightings")) and not s.get("paper_opened")
                                                  for s in live_signals),
        "signals_blocked_by_real_capacity": sum(a.get("status") == "REAL_CAPACITY_BLOCKED" for a in decisions),
        "observed_real_full_signals": sum(bool(s.get("real_full_sightings")) for s in live_signals),
        "shadow_real_buys": len(shadow_buys),
        "real_free_slot_opportunities": len(paper_full_real_free),
        "shadow_admission_status": _shadow_status(live),
        "last_capacity": live.get("last_capacity"),
        "note": "Counts only. Missed-trade profitability comes solely from the SHADOW replay below.",
    }


def _shadow_status(live):
    if live.get("shadow_real_admission") == "ACTIVE":
        return "ACTIVE"
    return "ACTIVE_AWAITING_NEXT_SCAN" if live.get("method") == "live_shadow_signal_recorder" else "NOT_STARTED"


def _capacity_architecture_report(signals, paths, live, now):
    paired = simulate_independent_capacity(signals, paths, lambda _signal: True, as_of=now)
    by_id = {s.get("signal_id"): s for s in signals if s.get("signal_id")}
    missed = [row for row in (live.get("shadow_real_admissions") or [])
              if row.get("status") == "SHADOW_REAL_BUY" and row.get("paper_full")]
    good = 0
    tracked = 0
    for row in missed:
        signal = by_id.get(row.get("signal_id")) or row.get("signal")
        if not isinstance(signal, dict):
            continue
        series = [point for point in paths.get(signal.get("token"), []) if point[0] <= now]
        if not series:
            continue
        result = _position(signal, series, None, None)
        tracked += int(result["tracked"])
        good += int(result["tracked"] and result["net"] > 0)
    a = lane_metrics(paired["architecture_a_real"]["trades"])
    b = lane_metrics(paired["architecture_b_real"]["trades"])
    return {
        "mode": "SHARED_SIGNAL_INDEPENDENT_PAPER_REAL_CAPACITY",
        "architecture_a": {"description": "REAL admission requires a PAPER position creation", "metrics": a},
        "architecture_b": {"description": "REAL admission evaluates every shared fresh BUY-ready signal", "metrics": b},
        "replay_difference": {"a_only_signals": len(paired["architecture_a_only"]),
                              "b_only_signals": len(paired["architecture_b_only"])},
        "observed_shadow": {"real_would_have_bought": len([a for a in (live.get("shadow_real_admissions") or [])
                                                             if a.get("status") == "SHADOW_REAL_BUY"]),
                            "paper_full_real_free": len(missed),
                            "good_tracked_opportunities_missed_by_a": good,
                            "tracked_missed_opportunities": tracked},
        "limits": paired["limits"],
        "real_transactions_from_shadow": 0,
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
                "dataset_complete_v2_paths": v2, "promotion_applied": False,
                "stale_profit_exit_research": stale_profit_research([], paths, as_of=now)}
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
        capacity_sim = simulate_independent_capacity(signals, paths, ch["accept"], as_of=now)
        results[ch["id"]] = {
            "label": ch["label"], "thresholds": ch.get("thresholds"),
            "thresholds_status": (None if "thresholds" not in ch else "DERIVED_FROM_TRAIN" if strict
                                  else "NOT_DERIVABLE_FROM_TRAIN (fewer than 4 single-quote TRAIN signals; equals Champion)"),
            "overall": overall,
            "independent_capacity_replay": {
                "paper": lane_metrics(capacity_sim["paper"]["trades"]),
                "real": lane_metrics(capacity_sim["architecture_b_real"]["trades"]),
                "architecture_a_real": lane_metrics(capacity_sim["architecture_a_real"]["trades"]),
                "architecture_b_only_signals": len(capacity_sim["architecture_b_only"]),
                "limits": capacity_sim["limits"]},
            "cost_sensitivity": cost, "by_lane": by_lane(trades), "counters": sim["counters"],
            "max_open_positions": sim["max_open"],
            "splits": {k: lane_metrics([t for t in trades if period(t) == k])
                       for k in ("train", "validation", "holdout")},
            "shadow": lane_metrics([t for t in trades if t.get("source") == "live_recorder"]),
            "trades": trades[-200:],
        }
    champ = results["A_CURRENT_CHAMPION"]
    capacity_architectures = _capacity_architecture_report(signals, paths, live, now)
    stale_research = stale_profit_research(signals, paths, as_of=now)
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
        "real_shadow_admission": {"status": _shadow_status(live),
                      "transactions": 0},
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
        "capacity_architectures": capacity_architectures,
        "stale_profit_exit_research": stale_research,
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
    champ_sim = simulate_independent_capacity(signals, stream_paths, lambda s: True, as_of=report_now)
    chall_sim = simulate_independent_capacity(
        signals, stream_paths, filter_challenger("buy", buy_filters)["accept"], as_of=report_now)
    champ = lane_metrics(champ_sim["architecture_b_real"]["trades"])
    chall = lane_metrics(chall_sim["architecture_b_real"]["trades"])
    champ_paper = lane_metrics(champ_sim["paper"]["trades"])
    chall_paper = lane_metrics(chall_sim["paper"]["trades"])
    reasons = []
    if chall["tracked_trades"] < 10:
        reasons.append(f"only {chall['tracked_trades']} counterfactual tracked trades")
    if (chall["net_pnl"] or 0) <= (champ["net_pnl"] or 0):
        reasons.append("counterfactual capacity replay net PnL not above Champion")
    if chall_paper["tracked_trades"] < 10:
        reasons.append(f"only {chall_paper['tracked_trades']} PAPER-capacity counterfactual tracked trades")
    if (chall_paper["net_pnl"] or 0) <= (champ_paper["net_pnl"] or 0):
        reasons.append("PAPER-capacity counterfactual net PnL not above Champion")
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
        stale = (lane_report or {}).get("stale_profit_exit_research") or {}
        return f"📊 BUY LANE RESEARCH\n\n{status} — {detail}.\n" \
               f"STALE_PROFIT_EXIT research: {stale.get('status', 'INSUFFICIENT_DATA')} (PAPER/SHADOW only)."
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
    architectures = lane_report.get("capacity_architectures") or {}
    observed = architectures.get("observed_shadow") or {}
    stale = lane_report.get("stale_profit_exit_research") or {}
    lines += ["", f"swing_single_quote: {assessment.get('verdict')}",
              f"Blocked (PAPER 10/10): {blocked.get('blocked_paper_10_of_10')} • "
              f"eligible while REAL 10/10: {blocked.get('eligible_while_real_10_of_10')}",
              f"SHADOW_REAL_BUY: {observed.get('real_would_have_bought', 0)} • "
              f"PAPER full / REAL free: {observed.get('paper_full_real_free', 0)} • "
              f"good A-missed: {observed.get('good_tracked_opportunities_missed_by_a', 0)}",
              f"Architecture A/B tracked REAL PnL: "
              f"{_fmt((architectures.get('architecture_a') or {}).get('metrics', {}).get('net_pnl'))} / "
              f"{_fmt((architectures.get('architecture_b') or {}).get('metrics', {}).get('net_pnl'))}",
              f"Stale-profit research: {stale.get('status', 'INSUFFICIENT_DATA')} (production REAL time exit OFF)"]
    for variant, metrics in (stale.get("variants") or {}).items():
        lines.append(f"{variant}: n={metrics.get('tracked_positions', 0)} "
                     f"PnL={_fmt(metrics.get('net_pnl_after_costs'))} "
                     f"stale exits={metrics.get('stale_profit_exits', 0)}")
    lines += [
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
