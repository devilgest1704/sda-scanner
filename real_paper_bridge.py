"""Opt-in reconciliation from the canonical PAPER ledger to the real executor.

The bridge is inert unless REAL_TRADING_ENABLED=true AND
REAL_TRADING_DRY_RUN=false. First run establishes a baseline and never
backfills existing PAPER positions. It only reacts to subsequent PAPER
position transitions. PAPER remains the sole signal/exit authority.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("real_paper_bridge_state.json")
EVENTS_FILE = Path("real_paper_bridge_events.json")
PAPER_FILE = Path("positions.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def _save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _paper_positions():
    ledger = _load(PAPER_FILE, {})
    positions = ledger.get("positions", {}) if isinstance(ledger, dict) else {}
    return {str(k).lower(): v for k, v in positions.items()
            if isinstance(v, dict) and str(k).startswith("0x")}


def reconcile():
    # Do not even import the executor while disabled.
    enabled = os.getenv("REAL_TRADING_ENABLED", "false").strip().lower() == "true"
    dry_run = os.getenv("REAL_TRADING_DRY_RUN", "true").strip().lower() == "true"
    if not enabled:
        return {"status": "disabled", "actions": []}
    if dry_run:
        # The bridge must not reconcile or initialize state in dry-run mode.
        # Use real_trader's explicit prepare_* routines separately for quote
        # validation; reconciliation is reserved for live-enabled operation.
        return {"status": "dry_run", "actions": []}

    # REAL must follow the exact active PAPER entry/exit profile. Fail closed
    # rather than trading a different strategy after a workflow config drift.
    paper_buy_mode = os.getenv("SDA_PAPER_BUY_MODE", "temporal_recovery").strip()
    paper_tp4 = os.getenv("SDA_PAPER_TP4", "0").strip() == "1"
    if paper_buy_mode != "temporal_recovery" or not paper_tp4:
        raise RuntimeError(
            "REAL bridge requires the active PAPER profile: "
            "SDA_PAPER_BUY_MODE=temporal_recovery and SDA_PAPER_TP4=1"
        )

    current = _paper_positions()
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("Invalid bridge state; refusing to overwrite it")
    else:
        state = {}
    if not state.get("initialized"):
        state = {"initialized": True, "paper_positions": sorted(current),
                 "initialized_at": _now(), "last_run": _now()}
        _save(STATE_FILE, state)
        return {"status": "baseline_initialized", "actions": []}

    previous = set(state.get("paper_positions", []))
    now_keys = set(current)
    import real_trading_config as cfg
    import real_trader
    import real_swing_sell_executor as swing_executor

    # Runtime migration archives old pending SELLs. Nothing from the legacy
    # bridge or old executor is replayed or broadcast during migration.
    migrated = swing_executor.migrate_if_needed()
    legacy_bridge = state.setdefault("legacy_pending_sell", [])
    for token in list(state.get("pending_sell", [])):
        legacy_bridge.append({"token": token, "archived_at": _now(),
                              "reason": "pre_swing_v2_bridge_sell_never_replayed"})
    state["pending_sell"] = []

    real_snapshot = real_trader.load_state()
    pending_buy = set(state.get("pending_buy", [])) | set(real_snapshot.get("pending_buys", {}))
    opened = sorted((now_keys - previous) | pending_buy)
    next_pending_buy = set()
    actions = []

    # Recover only post-migration Swing V2 intents. The executor checks their
    # persisted hashes/receipts and never blindly resubmits an UNKNOWN tx.
    try:
        for recovered in swing_executor.startup_recovery():
            actions.append({"action": "SELL_RECOVERY", **recovered, "at": _now()})
    except Exception as exc:
        actions.append({"action": "SELL_RECOVERY", "status": "ERROR",
                        "error": str(exc)[:500], "at": _now()})

    # New PAPER positions continue to create REAL BUYs exactly as before.
    for token in opened:
        pos = current.get(token, {})
        existing_pending = real_trader.load_state().get("pending_buys", {}).get(token, {})
        symbol = str(pos.get("symbol") or pos.get("ticker")
                      or existing_pending.get("symbol") or token[:8])
        paper_amount = pos.get("investment_sda", pos.get("amount_sda"))
        if paper_amount is None:
            paper_amount = cfg.POSITION_DEFAULT_SDA
        amount = cfg.position_sda(float(paper_amount))
        try:
            result = real_trader.execute_buy(token, symbol, amount)
            status = result.get("status", "UNKNOWN")
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": status, "tx": result.get("tx"), "at": _now()})
            if status not in ("CONFIRMED", "ALREADY_OPEN"):
                next_pending_buy.add(token)
        except Exception as exc:
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": "ERROR", "error": str(exc)[:500], "at": _now()})
            pending_state = real_trader.load_state().get("pending_buys", {})
            if token in pending_state:
                next_pending_buy.add(token)

    # REAL positions now exit only through fresh market-price observations and
    # the authoritative Swing SELL V2 policy. A PAPER close is NOT a SELL signal.
    if not migrated:
        try:
            import real_bot_menu
            real_state = real_trader.load_state()
            for token, real_pos in list(real_state.get("positions", {}).items()):
                if not isinstance(real_pos, dict):
                    continue
                symbol = str(real_pos.get("symbol") or token[:8])
                try:
                    market = real_bot_menu._newest_supabase_price(token)
                    price = market.get("price_sda") if isinstance(market, dict) else None
                    age = market.get("age_seconds") if isinstance(market, dict) else None
                    if price is None or market.get("stale") or age is None or float(age) > 300:
                        actions.append({"action": "SELL_DECISION", "token": token,
                                        "symbol": symbol, "status": "PRICE_UNAVAILABLE",
                                        "at": _now()})
                        continue
                    result = swing_executor.evaluate_position(
                        token, symbol, price, market.get("timestamp")
                    )
                    actions.append({"action": "SELL_DECISION", "token": token,
                                    "symbol": symbol, **result, "at": _now()})
                except Exception as exc:
                    actions.append({"action": "SELL_DECISION", "token": token,
                                    "symbol": symbol, "status": "ERROR",
                                    "error": str(exc)[:500], "at": _now()})
        except Exception as exc:
            actions.append({"action": "SELL_DECISION", "status": "ERROR",
                            "error": str(exc)[:500], "at": _now()})

    state["paper_positions"] = sorted(now_keys)
    state["pending_buy"] = sorted(next_pending_buy)
    state["pending_sell"] = []
    state["last_run"] = _now()
    state["last_actions"] = actions
    _save(STATE_FILE, state)
    if actions:
        events = _load(EVENTS_FILE, [])
        events.extend(actions)
        _save(EVENTS_FILE, events[-1000:])
    return {"status": "reconciled", "actions": actions,
            "paper_open": len(now_keys), "opened": len(opened),
            "closed": len(previous - now_keys), "sell_strategy": "swing_v2"}
