"""REAL wallet reconciliation and SELL V2 bridge. REAL BUY signals are independent."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("real_paper_bridge_state.json")
EVENTS_FILE = Path("real_paper_bridge_events.json")
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


def _safe_save_real_state(real_trader, state, wallet_sync, actions):
    try:
        real_trader.save_state(state)
        return True
    except Exception as exc:  # noqa: BLE001 - keep existing-position SELL management alive
        wallet_sync["ok"] = False
        wallet_sync["syncStatus"] = "DEGRADED"
        wallet_sync["syncError"] = f"REAL ledger write failed: {type(exc).__name__}: {str(exc)[:240]}"
        actions.append({"action": "WALLET_RECONCILIATION", "status": "FAILED",
                        "error": wallet_sync["syncError"], "at": _now()})
        return False


def _wallet_sync_summary(sync_result, real_trader):
    try:
        persisted = real_trader.load_state()
    except Exception:  # noqa: BLE001 - diagnostics must not stop independent SELL processing
        persisted = {}
    return {
        "syncStatus": sync_result.get("syncStatus") or persisted.get("syncStatus") or "UNKNOWN",
        "lastSyncAttemptAt": sync_result.get("lastSyncAttemptAt") or persisted.get("lastSyncAttemptAt"),
        "lastSuccessfulWalletSync": sync_result.get("lastSuccessfulWalletSync") or persisted.get("lastSuccessfulWalletSync"),
        "lastBlockchainSnapshotGeneratedAt": sync_result.get("lastBlockchainSnapshotGeneratedAt") or persisted.get("lastBlockchainSnapshotGeneratedAt"),
        "lastSuccessfulReconciliation": sync_result.get("lastSuccessfulReconciliation") or persisted.get("lastSuccessfulReconciliation"),
        "lastBlockchainDataAt": sync_result.get("lastBlockchainDataAt") or persisted.get("lastBlockchainDataAt"),
        "ledgerUpdatedAt": sync_result.get("ledgerUpdatedAt") or persisted.get("ledgerUpdatedAt"),
        "lastSyncError": sync_result.get("syncError") or persisted.get("lastSyncError"),
        "imported_count": len(sync_result.get("imported", [])),
    }


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


SNAPSHOT_REFRESH_TRIGGER_SECONDS = 5 * 60
SNAPSHOT_REFRESH_RETRY_SECONDS = 2 * 60
SNAPSHOT_FRESHNESS_LIMIT_SECONDS = 20 * 60


def _refresh_timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _request_snapshot_refresh_if_stale(real_trader, swing_executor):
    """Proactively refresh after 5m; retry every 2m until load + reconciliation.

    Dispatch is never treated as success. Success requires a newer snapshot to be
    loaded by the worker, FRESH wallet sync, and successful reconciliation. This
    path only dispatches the read-only collector and cannot block independent SELL
    management.
    """
    try:
        state = real_trader.load_state()
        if not isinstance(state, dict):
            return "STATE_UNAVAILABLE"
        now = datetime.now(timezone.utc)
        generated = _refresh_timestamp(state.get("lastBlockchainSnapshotGeneratedAt"))
        snapshot_age = (now - generated).total_seconds() if generated else None

        requested_from = _refresh_timestamp(state.get("accounting_refresh_requested_from_snapshot_at"))
        request_pending = requested_from is not None
        if request_pending:
            loaded_new_snapshot = generated is not None and generated > requested_from
            wallet_sync = _refresh_timestamp(state.get("lastSuccessfulWalletSync"))
            reconciliation = _refresh_timestamp(state.get("lastSuccessfulReconciliation"))
            fully_reconciled = (
                loaded_new_snapshot
                and str(state.get("syncStatus") or "").upper() == "FRESH"
                and wallet_sync is not None and wallet_sync >= generated
                and reconciliation is not None and reconciliation >= generated
            )
            if fully_reconciled:
                state.pop("accounting_refresh_requested_from_snapshot_at", None)
                state.pop("accounting_refresh_requested_at", None)
                state.pop("accounting_refresh_last_error", None)
                state["accounting_refresh_pending"] = False
                real_trader.save_state(state)
                return "REFRESH_CONFIRMED"
            last_attempt = _refresh_timestamp(state.get("accounting_refresh_last_attempt_at"))
            if last_attempt is not None and 0 <= (now - last_attempt).total_seconds() < SNAPSHOT_REFRESH_RETRY_SECONDS:
                return "RETRY_WAIT"

        elif snapshot_age is not None and 0 <= snapshot_age < SNAPSHOT_REFRESH_TRIGGER_SECONDS:
            wallet_sync = _refresh_timestamp(state.get("lastSuccessfulWalletSync"))
            reconciliation = _refresh_timestamp(state.get("lastSuccessfulReconciliation"))
            if (str(state.get("syncStatus") or "").upper() == "FRESH"
                    and wallet_sync is not None and wallet_sync >= generated
                    and reconciliation is not None and reconciliation >= generated):
                return "FRESH"

        # Missing/invalid/future timestamps fail closed and trigger collection.
        # Record the baseline before dispatch so a 204 response cannot be
        # mistaken for a refreshed snapshot. Every retry retains that baseline.
        if not request_pending:
            state["accounting_refresh_requested_from_snapshot_at"] = (
                generated.isoformat() if generated else now.isoformat()
            )
            state["accounting_refresh_requested_at"] = now.isoformat()
        state["accounting_refresh_pending"] = True
        result = swing_executor._request_accounting_refresh(state)
        return result if result in {"DISPATCHED", "THROTTLED"} else "RETRY_" + str(result)
    except Exception as exc:  # noqa: BLE001 - watchdog must not interrupt SELL management
        print(f"::warning::REAL accounting refresh watchdog failed: {type(exc).__name__}: {str(exc)[:180]}",
              file=sys.stderr, flush=True)
        return "FAILED"


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
    # The read-only blockchain accounting snapshot is the import authority for
    # newly discovered manual BUY lots. This path never signs or broadcasts.
    migrated = swing_executor.migrate_if_needed()
    refresh_dispatch = _request_snapshot_refresh_if_stale(real_trader, swing_executor)
    if refresh_dispatch not in {"FRESH", "DISPATCH_RECENT"}:
        print(f"REAL blockchain snapshot refresh: {refresh_dispatch}", flush=True)
    import real_wallet_reconciliation
    try:
        wallet_sync = real_wallet_reconciliation.reconcile_manual_buys(real_trader)
    except Exception as exc:  # noqa: BLE001 - keep independent SELL reconciliation alive
        wallet_sync = {"ok": False, "syncStatus": "FAILED",
                       "syncError": f"wallet reconciliation exception: {type(exc).__name__}: {str(exc)[:240]}",
                       "lastSyncAttemptAt": _now(), "imported": []}
    actions = []
    for imported in wallet_sync.get("imported", []):
        actions.append({"action": "MANUAL_BUY_IMPORT" if imported.get("source") == "MANUAL" else "BUY_RECONCILED",
                        "status": "OPEN", **imported,
                        "sellManagementEnabled": True, "at": _now()})
    if not wallet_sync.get("ok"):
        actions.append({"action": "WALLET_RECONCILIATION", "status": wallet_sync.get("syncStatus", "FAILED"),
                        "error": wallet_sync.get("syncError"), "at": _now()})
    actions.extend(_reconcile_real_wallets(real_trader, swing_executor))
    wallet_reconcile_errors = [a for a in actions
                               if a.get("action") in {"MANUAL_SELL_RECONCILIATION",
                                                     "WALLET_RECONCILIATION"}
                               and a.get("status") in {"ERROR", "FAILED", "STALE", "DEGRADED"}]
    snapshot_sync_ok = bool(wallet_sync.get("ok"))
    wallet_sync["ok"] = snapshot_sync_ok and not wallet_reconcile_errors
    real_state = real_trader.load_state()
    if wallet_sync["ok"]:
        real_state["lastSuccessfulReconciliation"] = _now()
        real_state["syncStatus"] = "FRESH"
        real_state["lastSyncError"] = None
        if _safe_save_real_state(real_trader, real_state, wallet_sync, actions):
            wallet_sync["lastSuccessfulReconciliation"] = real_state["lastSuccessfulReconciliation"]
    else:
        if snapshot_sync_ok:
            wallet_sync["syncStatus"] = "DEGRADED"
            wallet_sync["syncError"] = "; ".join(
                str(a.get("error") or a.get("status") or "wallet position reconciliation failed")
                for a in wallet_reconcile_errors
            ) or "wallet position reconciliation failed"
        real_state["syncStatus"] = wallet_sync.get("syncStatus") or "DEGRADED"
        real_state["lastSyncAttemptAt"] = wallet_sync.get("lastSyncAttemptAt") or real_state.get("lastSyncAttemptAt")
        real_state["lastSyncError"] = wallet_sync.get("syncError") or "wallet reconciliation failed"
        _safe_save_real_state(real_trader, real_state, wallet_sync, actions)
    pre_paper_action_count = len(actions)
    _persist_events(actions)

    # REAL BUY discovery is handled by tools.real_buy_pipeline. This bridge
    # owns only REAL wallet reconciliation and SELL V2; it must not inspect
    # PAPER positions, signals, capacity, cooldowns or risk state.
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise RuntimeError("Invalid REAL bridge state; refusing to overwrite it")
    else:
        state = {}
    pre_paper_action_count = len(actions)

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

    state["pending_sell"] = []
    state["last_run"] = _now()
    state["last_actions"] = actions
    health = sell_health(actions)
    state["last_real_reconciliation_at"] = state["last_run"]
    state["last_real_sell_evaluation_at"] = health["at"]
    state["wallet_sync"] = _wallet_sync_summary(wallet_sync, real_trader)
    state["real_positions_evaluated"] = health["evaluated"]
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
    status = "reconciled_with_sell_error" if health["health"] == "SELL_ERROR" else "reconciled"
    return {"status": status, "sell_health": health["health"],
            "sell_health_detail": health, "paper_state": "NOT_USED",
            "actions": actions, "sell_strategy": "swing_v2"}
