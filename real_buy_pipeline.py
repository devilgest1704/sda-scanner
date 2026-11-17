"""Independent REAL BUY admission and execution.

Consumes the scanner's market-only candidate snapshot. All temporal history,
cooldowns, risk accounting and deduplication are stored in
real_buy_signal_state.json. The module deliberately has no imports or file
references to the PAPER ledger, Shadow journal, optimizer or bridge state.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("real_buy_signal_state.json")
MARKET_FILE = Path("real_buy_market_snapshot.json")
REAL_LEDGER_TRADES_WINDOW_HOURS = 24
REAL_SIGNAL_LOSS_BUDGET_SDA = 20.0
REAL_SIGNAL_MIN_CLOSED_TRADES = 8
REAL_COOLDOWN_HOURS = 6.0
SCHEMA_VERSION = 1


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "signals": {},
        "cooldowns": {},
        "consumed_signal_ids": [],
        "last_scan_at": None,
        "last_diagnostics": {},
    }


def load_state(path=STATE_FILE):
    """Missing file initializes an empty state; malformed state fails closed."""
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"REAL BUY signal state unreadable; BUY disabled: {exc}") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("signals"), dict)
            or not isinstance(value.get("cooldowns"), dict)
            or not isinstance(value.get("consumed_signal_ids"), list)):
        raise RuntimeError("REAL BUY signal state invalid; BUY disabled")
    return value


def save_state(state, path=STATE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_market_snapshot(path=MARKET_FILE):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"REAL BUY market snapshot unavailable: {exc}") from exc
    updated = _stamp(value.get("updated_at")) if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("candidates"), list) or updated is None):
        raise RuntimeError("REAL BUY market snapshot invalid")
    age = ((_now() - updated).total_seconds())
    source_at = _stamp(value.get("market_source_at"))
    source_age = ((_now() - source_at).total_seconds()) if source_at else None
    if age < 0 or age > 300 or source_age is None or source_age < 0 or source_age > 300:
        raise RuntimeError("REAL BUY market snapshot stale or source timestamp invalid")
    return value


def real_loss_circuit(real_state, at=None):
    """REAL-only rolling 24h realized-loss circuit; fail closed on bad REAL rows."""
    now = at or _now()
    floor = now - timedelta(hours=REAL_LEDGER_TRADES_WINDOW_HOURS)
    recent = []
    trades = real_state.get("trades") if isinstance(real_state, dict) else None
    if not isinstance(trades, list):
        return {"blocked": True, "reason": "REAL_TRADE_HISTORY_INVALID"}
    for trade in trades:
        if not isinstance(trade, dict) or str(trade.get("action") or "").upper() != "SELL":
            continue
        stamp = _stamp(trade.get("at") or trade.get("closed_at"))
        if stamp is None or stamp > now or stamp < floor:
            continue
        pnl = _number(trade.get("realized_pnl_sda"))
        if pnl is None:
            return {"blocked": True, "reason": "REAL_RECENT_PNL_INVALID"}
        recent.append(pnl)
    total = sum(recent)
    blocked = total <= -REAL_SIGNAL_LOSS_BUDGET_SDA
    return {"blocked": blocked, "reason": "REAL_24H_LOSS_BUDGET" if blocked else "REAL_RISK_OK",
            "window_closed": len(recent), "window_pnl_sda": round(total, 4),
            "minimum_closed": REAL_SIGNAL_MIN_CLOSED_TRADES}


def _sync_real_cooldowns(signal_state, real_state, at):
    """Rebuild REAL stop cooldowns only from REAL completed SELL records."""
    cooldowns = signal_state.setdefault("cooldowns", {})
    for trade in real_state.get("trades", []) if isinstance(real_state.get("trades"), list) else []:
        if not isinstance(trade, dict) or str(trade.get("action") or "").upper() != "SELL":
            continue
        reason = str(trade.get("sell_reason") or trade.get("close_reason") or trade.get("reason") or "").upper()
        if not any(marker in reason for marker in ("STOP", "WEAK", "STALE", "BREAKDOWN")):
            continue
        token = str(trade.get("token") or "").lower()
        stamp = _stamp(trade.get("at") or trade.get("closed_at"))
        if token and stamp and stamp <= at:
            until = (stamp + timedelta(hours=REAL_COOLDOWN_HOURS)).isoformat()
            if until > str(cooldowns.get(token) or ""):
                cooldowns[token] = until
    return cooldowns


def _candidate_signal(candidate, previous, at):
    """Run the exact TP4 temporal market rules against REAL-owned prior evidence."""
    from tools.temporal_recovery_buy import evaluate_tp4

    token = str(candidate.get("address") or candidate.get("contract") or "").lower()
    current = {
        "phase": candidate.get("phase"),
        "score": candidate.get("score"),
        "quality": candidate.get("quality"),
        "impulse": candidate.get("impulse"),
        "quote_at": candidate.get("quote_at"),
        "data_age_sec": candidate.get("data_age_sec"),
        "price_sda": candidate.get("price_sda"),
        "m1h": candidate.get("m1h"),
        "m15": candidate.get("m15"),
        "volume_1h": candidate.get("volume_1h"),
        "flow_1h": candidate.get("flow_1h"),
        "flow_15m": candidate.get("flow_15m"),
        "trades_1h": candidate.get("trades_1h"),
        "buy_ratio": candidate.get("buy_ratio"),
    }
    temporal = evaluate_tp4(token, current, at=at, previous=previous if isinstance(previous, dict) else {})
    return current, temporal


def admit_candidate(candidate, temporal, signal_state, real_state, cfg, safety_check,
                    at=None):
    """REAL-only state adapter. Returns eligibility and the actual REAL blocker."""
    now = at or _now()
    token = str(candidate.get("address") or candidate.get("contract") or "").lower()
    symbol = str(candidate.get("symbol") or token[:10])
    if not token.startswith("0x") or len(token) != 42:
        return {"ready": False, "status": "BLOCKED", "reason": "INVALID_TOKEN_ADDRESS"}
    if not temporal.get("ready"):
        return {"ready": False, "status": "WATCH",
                "reason": str(temporal.get("reason") or "NO_VALID_TEMPORAL_SIGNAL")}
    quote = _stamp(candidate.get("quote_at"))
    age = _number(candidate.get("data_age_sec"))
    if quote is None or age is None or not 0 <= age <= 300 or not 0 <= (now - quote).total_seconds() <= 300:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_DATA_STALE"}
    m1 = _number(candidate.get("m1h"))
    m15 = _number(candidate.get("m15"))
    flow = _number(candidate.get("flow_1h"))
    volume = _number(candidate.get("volume_1h"))
    trades = _number(candidate.get("trades_1h"))
    ratio = _number(candidate.get("buy_ratio"))
    if any(value is None for value in (m1, m15, flow, volume, trades, ratio)):
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_MARKET_METRICS_MISSING"}
    if m1 > 12:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_LATE_SPIKE"}
    if m1 >= 12 and flow / max(volume, 1.0) < 0.18:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_PRICE_FLOW_MISMATCH"}
    if trades < 12 and temporal.get("sparse_activity_verified") is not True:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_V31_ACTIVITY"}
    macd = candidate.get("macd_filter")
    if isinstance(macd, dict) and macd.get("blocked") is True:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_MACD_BEARISH"}
    cooldown = _stamp((signal_state.get("cooldowns") or {}).get(token))
    if cooldown and cooldown > now:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_COOLDOWN",
                "cooldown_until": cooldown.isoformat()}
    risk = real_loss_circuit(real_state, now)
    if risk["blocked"]:
        return {"ready": False, "status": "BLOCKED", "reason": risk["reason"], "risk": risk}
    positions = real_state.get("positions") if isinstance(real_state.get("positions"), dict) else {}
    pending = real_state.get("pending_buys") if isinstance(real_state.get("pending_buys"), dict) else {}
    if token in positions or token in pending:
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_TOKEN_ALREADY_OPEN_OR_PENDING"}
    amount = float(cfg.position_sda())
    try:
        safety_check(amount, real_state)
    except Exception as exc:
        return {"ready": False, "status": "BLOCKED",
                "reason": f"REAL_SAFETY: {str(exc)[:180]}", "risk": risk}
    signal_id = hashlib.sha256(
        f"{token}|{candidate.get('quote_at')}|{candidate.get('strategy_version') or 'V30'}|REAL".encode()
    ).hexdigest()
    if signal_id in signal_state.get("consumed_signal_ids", []):
        return {"ready": False, "status": "BLOCKED", "reason": "REAL_DUPLICATE_SIGNAL"}
    return {"ready": True, "status": "BUY_READY", "reason": "REAL_SAFETY_PASS",
            "token": token, "symbol": symbol, "amount_sda": amount,
            "signal_id": signal_id, "risk": risk}


def run(market_path=MARKET_FILE, state_path=STATE_FILE, *, at=None):
    """Evaluate current scanner candidates and execute only after REAL safety passes."""
    now = at or _now()
    if os.getenv("SDA_SCANNER_BUY_DATA_AVAILABLE", "true").strip().lower() != "true":
        return {"status": "BUY_DATA_DEGRADED", "buy_ready": 0, "watch": 0,
                "blocker": "REQUIRED_SCANNER_SOURCE_DEGRADED"}
    try:
        market = load_market_snapshot(market_path)
        signal_state = load_state(state_path)
    except Exception as exc:
        return {"status": "BUY_DATA_DEGRADED", "buy_ready": 0, "watch": 0,
                "blocker": f"FAIL_CLOSED: {type(exc).__name__}: {str(exc)[:180]}"}

    import real_trading_config as cfg
    import real_trader

    try:
        real_state = real_trader.load_state()
        if not isinstance(real_state, dict):
            raise RuntimeError("REAL ledger invalid")
    except Exception as exc:
        return {"status": "BUY_DATA_DEGRADED", "buy_ready": 0, "watch": 0,
                "blocker": f"REAL_LEDGER_UNAVAILABLE: {str(exc)[:180]}"}

    _sync_real_cooldowns(signal_state, real_state, now)
    rows = market["candidates"]
    diagnostics = {}
    actions = []
    buy_ready = 0
    watch = 0
    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        token = str(candidate.get("address") or candidate.get("contract") or "").lower()
        if not token:
            continue
        previous = signal_state["signals"].get(token)
        current, temporal = _candidate_signal(candidate, previous, now)
        # Persist every observed REAL signal independently of PAPER/Shadow state.
        signal_state["signals"][token] = {
            **current, "decision_at": now.isoformat(),
            "phase": current.get("phase"), "score": current.get("score"),
            "quality": current.get("quality"), "impulse": current.get("impulse"),
        }
        result = admit_candidate(candidate, temporal, signal_state, real_state,
                                 cfg, real_trader.safety_check, now)
        diagnostics[token] = {
            "symbol": candidate.get("symbol"), "status": result["status"],
            "buy_ready": result["ready"], "blocker": result["reason"],
            "score": candidate.get("score"), "quality": candidate.get("quality"),
            "impulse": candidate.get("impulse"), "phase": candidate.get("phase"),
            "data_age_sec": candidate.get("data_age_sec"),
            "temporal": temporal, "quote_at": candidate.get("quote_at"),
        }
        if result["ready"]:
            buy_ready += 1
        else:
            watch += 1
        if result["ready"]:
            signal_state.setdefault("consumed_signal_ids", []).append(result["signal_id"])
            signal_state["consumed_signal_ids"] = signal_state["consumed_signal_ids"][-20000:]
            save_state(signal_state, state_path)
            if cfg.REAL_TRADING_ENABLED and not cfg.REAL_TRADING_DRY_RUN:
                try:
                    executed = real_trader.execute_buy(
                        result["token"], result["symbol"], result["amount_sda"]
                    )
                    actions.append({"token": token, "status": executed.get("status", "UNKNOWN"),
                                    "tx": executed.get("tx")})
                    real_state = real_trader.load_state()
                except Exception as exc:
                    actions.append({"token": token, "status": "ERROR", "error": str(exc)[:240]})
            else:
                actions.append({"token": token, "status": "WOULD_EXECUTE",
                                "amount_sda": result["amount_sda"]})
    signal_state["last_scan_at"] = now.isoformat()
    signal_state["last_diagnostics"] = diagnostics
    signal_state["summary"] = {
        "buy_ready": buy_ready, "watch": watch,
        "available_sda": round(max(0.0, cfg.TRADING_WALLET_CAPITAL_SDA -
                                    float(real_trader.deployed_cost_basis(real_state))), 4),
        "next_buy_sda": cfg.position_sda(),
        "positions": len(real_state.get("positions") or {}) + len(real_state.get("pending_buys") or {}),
        "max_positions": cfg.MAX_OPEN_POSITIONS,
        "updated_at": now.isoformat(),
    }
    save_state(signal_state, state_path)
    return {"status": "ok", "buy_ready": buy_ready, "watch": watch,
            "summary": signal_state["summary"], "actions": actions}
