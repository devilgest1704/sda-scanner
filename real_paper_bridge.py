"""Opt-in reconciliation from the canonical PAPER ledger to the real executor.

The bridge is inert unless REAL_TRADING_ENABLED=true AND
REAL_TRADING_DRY_RUN=false. First run establishes a baseline and never
backfills existing PAPER positions. It only reacts to subsequent PAPER
position transitions. PAPER remains the sole signal/exit authority.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("real_paper_bridge_state.json")
EVENTS_FILE = Path("real_paper_bridge_events.json")
PAPER_FILE = Path("positions.json")

MAX_PAPER_BUY_AGE_SECONDS = 15 * 60
PAPER_CLOCK_SKEW_SECONDS = 60
REAL_LIMIT_ERRORS = (
    "Maximum real open positions reached",
    "Real trading capital cap",
    "Daily real-trading loss limit reached",
)

SELL_ACTIONS = {"SELL_DECISION", "SELL_RECOVERY", "MANUAL_SELL_RECONCILIATION", "ACCOUNTING_REPAIR",
                "DUST_RECONCILIATION"}
SELL_ERROR_STATUSES = {
    "ERROR", "FAILED", "DROPPED", "QUOTE_FAILED", "SWAP_REVERTED", "APPROVAL_REVERTED",
    "BROADCAST_PREPARATION_FAILED", "PRE_BROADCAST_ABORTED", "ACCOUNTING_UNVERIFIED",
    "UNWRAP_FAILED", "UNWRAP_DROPPED",
}
SELL_QUARANTINE_STATUSES = {"QUARANTINED", "SELL_QUARANTINED"}
SELL_PENDING_STATUSES = {
    "PENDING_CONFIRMATION", "UNKNOWN", "SELL_BACKOFF", "SELL_INTENT_PENDING",
    "ALLOWANCE_NOT_CONFIRMED", "UNWRAP_DEFERRED", "INTENT_ALREADY_EXISTS", "DRY_RUN",
}


def sell_health(actions):
    """Summarize SELL subsystem health; any SELL error is never reported as healthy."""
    rows = [a for a in actions if isinstance(a, dict) and a.get("action") in SELL_ACTIONS]
    statuses = [str(a.get("status") or "") for a in rows]
    errors = [a for a in rows if str(a.get("status") or "") in SELL_ERROR_STATUSES]
    counts = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    if errors:
        health = "SELL_ERROR"
    elif any(s in SELL_QUARANTINE_STATUSES for s in statuses):
        health = "SELL_QUARANTINED"
    elif any(s == "CONFIRMED" for s in statuses):
        health = "SELL_EXECUTED"
    elif any(s in SELL_PENDING_STATUSES for s in statuses):
        health = "SELL_PENDING"
    else:
        health = "SELL_HOLD"
    return {"health": health, "evaluated": len(rows), "status_counts": counts,
            "errors": [{"token": a.get("token"), "symbol": a.get("symbol"),
                        "status": a.get("status"), "error": a.get("error") or a.get("failure_code")}
                       for a in errors][:20],
            "at": _now()}


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


def _persist_events(actions):
    if not actions:
        return
    events = _load(EVENTS_FILE, [])
    events.extend(actions)
    _save(EVENTS_FILE, events[-1000:])


def _reconcile_real_wallets(real_trader, swing_executor):
    actions = []
    try:
        positions = real_trader.load_state().get("positions", {})
    except Exception as exc:
        return [{"action": "MANUAL_SELL_RECONCILIATION", "status": "ERROR",
                 "error": f"{type(exc).__name__}: {str(exc)[:400]}", "at": _now()}]
    for token, position in list(positions.items()):
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or token[:8])
        try:
            result = swing_executor.reconcile_position_wallet(token)
            status = result.get("status")
            if status == "CLOSED_MANUAL_RECONCILED":
                actions.append({"action": "MANUAL_SELL_RECONCILIATION", "token": token,
                                "symbol": symbol, "status": "CLOSED", "at": _now()})
            elif status == "MANUAL_RECONCILED":
                actions.append({"action": "MANUAL_SELL_RECONCILIATION", "token": token,
                                "symbol": symbol, "status": "PARTIAL", "at": _now()})
            elif status == "DUST_RECONCILED":
                if result.get("manual_disposal"):
                    actions.append({"action": "MANUAL_SELL_RECONCILIATION", "token": token,
                                    "symbol": symbol, "status": "CLOSED", "at": _now()})
                actions.append({"action": "DUST_RECONCILIATION", "token": token,
                                "symbol": symbol, "status": "DUST_CLOSED",
                                "wallet_quantity_raw": result.get("wallet_quantity"),
                                "reason": "NEGLIGIBLE_RESIDUAL_AFTER_DISPOSAL", "at": _now()})
        except Exception as exc:
            actions.append({"action": "MANUAL_SELL_RECONCILIATION", "token": token,
                            "symbol": symbol, "status": "ERROR",
                            "error": f"{type(exc).__name__}: {str(exc)[:400]}", "at": _now()})
    return actions


def _paper_positions():
    """Return (positions, None) for a valid PAPER ledger, else (None, reason)."""
    try:
        ledger = json.loads(PAPER_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "PAPER_FILE_MISSING"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"PAPER_FILE_UNREADABLE: {type(exc).__name__}"
    except ValueError as exc:
        return None, f"PAPER_JSON_MALFORMED: {str(exc)[:120]}"
    if not isinstance(ledger, dict):
        return None, "PAPER_LEDGER_NOT_A_DICT"
    if "positions" not in ledger:
        return None, "PAPER_POSITIONS_MISSING"
    positions = ledger["positions"]
    if not isinstance(positions, dict):
        return None, "PAPER_POSITIONS_WRONG_TYPE"
    return {str(k).lower(): v for k, v in positions.items()
            if isinstance(v, dict) and str(k).startswith("0x")}, None


def _paper_age_seconds(position, now):
    """Age of a PAPER entry; None when opened_at is missing, invalid or in the future."""
    try:
        opened = datetime.fromisoformat(str(position.get("opened_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if opened.tzinfo is None:
        return None
    age = (now - opened).total_seconds()
    return age if age >= -PAPER_CLOCK_SKEW_SECONDS else None


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

    import real_trading_config as cfg
    import real_trader
    import real_swing_sell_executor as swing_executor

    # Wallet reconciliation is REAL-state work, not PAPER signal processing.
    migrated = swing_executor.migrate_if_needed()
    actions = _reconcile_real_wallets(real_trader, swing_executor)
    pre_paper_action_count = len(actions)
    _persist_events(actions)

    # REAL must follow the exact active PAPER entry/exit profile. Fail closed
    # rather than trading a different strategy after a workflow config drift.
    paper_buy_mode = os.getenv("SDA_PAPER_BUY_MODE", "temporal_recovery").strip()
    paper_tp4 = os.getenv("SDA_PAPER_TP4", "0").strip() == "1"
    if paper_buy_mode != "temporal_recovery" or not paper_tp4:
        raise RuntimeError(
            "REAL bridge requires the active PAPER profile: "
            "SDA_PAPER_BUY_MODE=temporal_recovery and SDA_PAPER_TP4=1"
        )

    current, paper_error = _paper_positions()
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("Invalid bridge state; refusing to overwrite it")
    else:
        state = {}
    if not state.get("initialized"):
        if paper_error:
            # Never create a baseline from an unknown PAPER state.
            actions.append({"action": "PAPER_STATE_UNAVAILABLE", "status": "BUY_DISCOVERY_SKIPPED",
                            "reason": paper_error, "at": _now()})
            _persist_events(actions[pre_paper_action_count:])
            return {"status": "paper_state_unavailable", "paper_state_error": paper_error,
                    "actions": actions}
        state = {"initialized": True, "paper_positions": sorted(current),
                 "initialized_at": _now(), "last_run": _now()}
        _save(STATE_FILE, state)
        return {"status": "baseline_initialized", "actions": actions}

    previous = set(state.get("paper_positions", []))
    paper_valid = paper_error is None
    now_keys = set(current) if paper_valid else set(previous)
    legacy_bridge = state.setdefault("legacy_pending_sell", [])
    for token in list(state.get("pending_sell", [])):
        legacy_bridge.append({"token": token, "archived_at": _now(),
                              "reason": "pre_swing_v2_bridge_sell_never_replayed"})
    state["pending_sell"] = []

    real_snapshot = real_trader.load_state()
    pending_buy = set(state.get("pending_buy", [])) | set(real_snapshot.get("pending_buys", {}))
    newly_seen = now_keys - previous
    opened = sorted(newly_seen | pending_buy) if paper_valid else []
    next_pending_buy = set() if paper_valid else set(state.get("pending_buy", []))
    detected_at = datetime.now(timezone.utc)
    if not paper_valid:
        # BUY discovery is skipped; REAL SELL V2 below still uses REAL state only.
        failures = int((state.get("paper_state") or {}).get("consecutive_failures") or 0) + 1
        state["paper_state"] = {"status": "UNAVAILABLE", "reason": paper_error,
                                "consecutive_failures": failures, "at": _now()}
        actions.append({"action": "PAPER_STATE_UNAVAILABLE", "status": "BUY_DISCOVERY_SKIPPED",
                        "reason": paper_error, "preserved_baseline_size": len(previous),
                        "at": _now()})
        print(f"::warning::PAPER_STATE_UNAVAILABLE ({paper_error}); REAL BUY discovery skipped, "
              "previous PAPER baseline preserved", file=sys.stderr, flush=True)
    else:
        state["paper_state"] = {"status": "VALID", "consecutive_failures": 0, "at": _now()}

    # Recover only post-migration Swing V2 intents. The executor checks their
    # persisted hashes/receipts and never blindly resubmits an UNKNOWN tx.
    try:
        for recovered in swing_executor.startup_recovery():
            label = "ACCOUNTING_REPAIR" if recovered.get("action") == "ACCOUNTING_REPAIR" else "SELL_RECOVERY"
            actions.append({**recovered, "action": label,
                            "result_action": recovered.get("action"), "at": _now()})
    except Exception as exc:
        actions.append({"action": "SELL_RECOVERY", "status": "ERROR",
                        "error": str(exc)[:500], "at": _now()})

    # New PAPER positions continue to create REAL BUYs exactly as before.
    for token in opened:
        pos = current.get(token, {})
        existing_pending = real_trader.load_state().get("pending_buys", {}).get(token, {})
        symbol = str(pos.get("symbol") or pos.get("ticker")
                      or existing_pending.get("symbol") or token[:8])
        if token in newly_seen and token not in pending_buy:
            age = _paper_age_seconds(pos, detected_at)
            if age is None or age > MAX_PAPER_BUY_AGE_SECONDS:
                # Delayed discovery: never enter at a different market condition later.
                actions.append({"action": "BUY", "token": token, "symbol": symbol,
                                "status": "STALE_PAPER_BUY_SKIPPED",
                                "paper_opened_at": pos.get("opened_at"),
                                "detected_at": detected_at.isoformat(),
                                "age_seconds": age, "max_age_seconds": MAX_PAPER_BUY_AGE_SECONDS,
                                "at": _now()})
                continue
        # REAL BUY size is the configured production size, not the PAPER research size.
        amount = cfg.position_sda()
        try:
            result = real_trader.execute_buy(token, symbol, amount)
            status = result.get("status", "UNKNOWN")
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": status, "tx": result.get("tx"), "at": _now()})
            if status not in ("CONFIRMED", "ALREADY_OPEN"):
                next_pending_buy.add(token)
        except Exception as exc:
            message = str(exc)
            limited = any(marker in message for marker in REAL_LIMIT_ERRORS)
            # REAL-limit skips are final: the PAPER signal is never replayed later.
            actions.append({"action": "BUY", "token": token, "symbol": symbol,
                           "status": "PAPER_BUY_SKIPPED_REAL_LIMIT" if limited else "ERROR",
                           "error": message[:500], "at": _now()})
            pending_state = real_trader.load_state().get("pending_buys", {})
            if token in pending_state:
                next_pending_buy.add(token)

    # REAL positions now exit only through fresh market-price observations and
    # the authoritative Swing SELL V2 policy. A PAPER close is NOT a SELL signal.
    if not migrated:
        try:
            # Production SELL decisions use the same standalone market-price
            # resolver as the read-only REAL portfolio and SELL V2 preview.
            # Telegram/menu modules are deliberately absent from this path.
            import real_market_price as market_prices
            market_snapshot = market_prices.load_market_snapshot()
            token_metadata = market_prices.load_token_metadata()
            real_state = real_trader.load_state()

            def reference_quote(token):
                item = token_metadata.get(str(token).lower())
                if not isinstance(item, dict):
                    return None
                try:
                    decimals = int(item["decimals"])
                    if not 0 <= decimals <= 36:
                        return None
                    wallet = real_trader.wallet_address()
                    amount_in = 10 ** 18
                    fee, amount_out_raw, _calldata, _deadline = real_trader.quote_best_live_fee(
                        cfg.WSDA_ADDRESS, token, token, wallet, amount_in,
                        value_wei=amount_in,
                    )
                    amount_out_raw = int(amount_out_raw)
                    if amount_out_raw <= 0:
                        return None
                    # Read-only 1 SDA BUY-side mark, never an executable SELL quote.
                    price = (10 ** decimals) / amount_out_raw
                    return {
                        "price_sda": price,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": "PinetSwap 1 SDA BUY-side reference quote",
                        "quote_amount_sda": 1,
                        "quote_side": "BUY_REFERENCE",
                        "fee_tier": int(fee),
                    }
                except Exception:
                    return None

            for token, real_pos in list(real_state.get("positions", {}).items()):
                if not isinstance(real_pos, dict):
                    continue
                symbol = str(real_pos.get("symbol") or token[:8])
                try:
                    # A known automatic SELL is finalized before the wallet may be compared.
                    reconciliation = swing_executor.reconcile_position(token)
                    intent_result = reconciliation.get("intent")
                    if intent_result:
                        actions.append({**intent_result, "action": "SELL_RECOVERY",
                                        "result_action": intent_result.get("action"),
                                        "token": token, "symbol": symbol, "at": _now()})
                    wallet_reconciliation = reconciliation.get("wallet") or {}
                    if wallet_reconciliation.get("status") == "CLOSED_MANUAL_RECONCILED":
                        actions.append({"action": "MANUAL_SELL_RECONCILIATION",
                                        "token": token, "symbol": symbol,
                                        "status": "CLOSED", "at": _now()})
                        continue
                    if wallet_reconciliation.get("status") == "MANUAL_RECONCILED":
                        actions.append({"action": "MANUAL_SELL_RECONCILIATION",
                                        "token": token, "symbol": symbol,
                                        "status": "PARTIAL", "at": _now()})
                    if wallet_reconciliation.get("status") == "DUST_RECONCILED":
                        if wallet_reconciliation.get("manual_disposal"):
                            actions.append({"action": "MANUAL_SELL_RECONCILIATION",
                                            "token": token, "symbol": symbol,
                                            "status": "CLOSED", "at": _now()})
                        actions.append({"action": "DUST_RECONCILIATION", "token": token,
                                        "symbol": symbol, "status": "DUST_CLOSED",
                                        "wallet_quantity_raw": wallet_reconciliation.get("wallet_quantity"),
                                        "reason": "NEGLIGIBLE_RESIDUAL_AFTER_DISPOSAL",
                                        "at": _now()})
                        continue
                    if wallet_reconciliation.get("status") == "SELL_INTENT_ACTIVE":
                        actions.append({"action": "SELL_DECISION", "token": token,
                                        "symbol": symbol, "status": "SELL_INTENT_PENDING",
                                        "stage": wallet_reconciliation.get("stage"), "at": _now()})
                        continue
                    if wallet_reconciliation.get("status") == "NO_POSITION":
                        continue
                    market = market_prices.resolve_market_price(
                        token, market_snapshot,
                        now=datetime.now(timezone.utc),
                        supabase_lookup=market_prices.newest_supabase_price,
                        reference_quote_lookup=reference_quote,
                    )
                    if market is None:
                        actions.append({"action": "SELL_DECISION", "token": token,
                                        "symbol": symbol, "status": "PRICE_UNAVAILABLE",
                                        "at": _now()})
                        continue
                    result = swing_executor.evaluate_position(
                        token, symbol, market["price_sda"], market.get("timestamp"),
                        wallet_reconciled=True
                    )
                    actions.append({"action": "SELL_DECISION", "token": token,
                                    "symbol": symbol,
                                    "decision_price": market["price_sda"],
                                    "price_timestamp": market.get("timestamp"),
                                    "price_age_seconds": market.get("age_seconds"),
                                    "price_source": market.get("source"),
                                    **{key: value for key, value in result.items() if key != "action"},
                                    "decision_action": result.get("action"),
                                    "at": _now()})
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
    health = sell_health(actions)
    if migrated:
        health["note"] = "MIGRATION_BASELINE"
    state["sell_health"] = health
    _save(STATE_FILE, state)
    _persist_events(actions[pre_paper_action_count:])
    if health["health"] == "SELL_ERROR":
        print(f"::error::REAL SELL HEALTH SELL_ERROR {json.dumps(health['errors'], default=str)[:1500]}",
              file=sys.stderr, flush=True)
    elif health["health"] == "SELL_QUARANTINED":
        print(f"::warning::REAL SELL HEALTH SELL_QUARANTINED {json.dumps(health['status_counts'])}",
              file=sys.stderr, flush=True)
    if health["health"] == "SELL_ERROR":
        status = "reconciled_with_sell_error"
    elif not paper_valid:
        status = "paper_state_unavailable"
    else:
        status = "reconciled"
    return {"status": status,
            "sell_health": health["health"], "sell_health_detail": health,
            "paper_state": "VALID" if paper_valid else "UNAVAILABLE",
            "paper_state_error": paper_error,
            "actions": actions,
            "paper_open": len(now_keys), "opened": len(opened),
            "closed": len(previous - now_keys), "sell_strategy": "swing_v2"}
