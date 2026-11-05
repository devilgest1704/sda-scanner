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

    current = _paper_positions()
    state = _load(STATE_FILE, {})
    if not state.get("initialized"):
        state = {"initialized": True, "paper_positions": sorted(current),
                 "initialized_at": _now(), "last_run": _now()}
        _save(STATE_FILE, state)
        return {"status": "baseline_initialized", "actions": []}

    previous = set(state.get("paper_positions", []))
    now_keys = set(current)
    # Keep reconciling an uncertain BUY even if PAPER has already exited it.
    # Once its receipt confirms, immediately mirror that already-committed
    # PAPER close with a real SELL rather than orphaning the real token lot.
    pending_buy = set(state.get("pending_buy", []))
    pending_sell = set(state.get("pending_sell", []))
    opened = sorted((now_keys - previous) | pending_buy)
    closed = sorted((previous - now_keys) | pending_sell)
    confirmed_after_paper_close = set()
    next_pending_buy = set()
    next_pending_sell = set()
    actions = []
    import real_trading_config as cfg
    import real_trader

    # New PAPER positions become real buys only after the paper ledger commits.
    for token in opened:
        pos = current.get(token, {})
        existing_pending = real_trader.load_state().get("pending_buys", {}).get(token, {})
        symbol = str(pos.get("symbol") or pos.get("ticker")
                      or existing_pending.get("symbol") or token[:8])
        amount = cfg.position_sda()
        try:
            result = real_trader.execute_buy(token, symbol, amount)
            status = result.get("status", "UNKNOWN")
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": status, "tx": result.get("tx"), "at": _now()})
            if status not in ("CONFIRMED", "ALREADY_OPEN"):
                if token in now_keys:
                    next_pending_buy.add(token)
            elif token not in now_keys:
                confirmed_after_paper_close.add(token)
        except Exception as exc:
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": "ERROR", "error": str(exc)[:500], "at": _now()})
            if token in now_keys:
                next_pending_buy.add(token)

    closed = sorted(set(closed) | confirmed_after_paper_close)

    # A PAPER close is mirrored only for a token that has a recorded real lot.
    real_state = real_trader.load_state()
    for token in closed:
        real_pos = real_state.get("positions", {}).get(token)
        if not isinstance(real_pos, dict):
            actions.append({"action": "SELL", "token": token,
                           "status": "NO_REAL_POSITION", "at": _now()})
            continue
        amount_raw = int(real_pos.get("token_amount_raw") or 0)
        if amount_raw <= 0:
            actions.append({"action": "SELL", "token": token,
                           "status": "MISSING_TOKEN_AMOUNT", "at": _now()})
            next_pending_sell.add(token)
            continue
        try:
            result = real_trader.execute_sell(
                token, str(real_pos.get("symbol") or token[:8]), amount_raw)
            status = result.get("status", "UNKNOWN")
            actions.append({"action": "SELL", "token": token, "status": status,
                           "tx": result.get("tx"), "at": _now()})
            if status != "CONFIRMED":
                next_pending_sell.add(token)
        except Exception as exc:
            actions.append({"action": "SELL", "token": token,
                           "status": "ERROR", "error": str(exc)[:500], "at": _now()})
            next_pending_sell.add(token)

    state["paper_positions"] = sorted(now_keys)
    state["pending_buy"] = sorted(next_pending_buy)
    state["pending_sell"] = sorted(next_pending_sell)
    state["last_run"] = _now()
    state["last_actions"] = actions
    _save(STATE_FILE, state)
    if actions:
        events = _load(EVENTS_FILE, [])
        events.extend(actions)
        _save(EVENTS_FILE, events[-1000:])
    return {"status": "reconciled", "actions": actions,
            "paper_open": len(now_keys), "opened": len(opened), "closed": len(closed)}
