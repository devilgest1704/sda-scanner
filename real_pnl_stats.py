"""Read-only REAL BOT PnL model shared by every Telegram REAL screen.

Realized events are the blockchain/FIFO-verified BOT SELL transactions from
``real_accounting_state.json``. Open PnL is supplied by the caller from the
current remaining BOT positions. Nothing here signs, quotes or broadcasts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

try:
    from zoneinfo import ZoneInfo
    _PRAGUE = ZoneInfo("Europe/Prague")
except Exception:
    _PRAGUE = None

PAPER_SOURCES = {"PAPER", "PAPER_TRADE", "SIMULATION"}
EXCLUDED_SYMBOLS = {"PINE"}


def _last_sunday(year, month):
    day = date(year, month, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def to_prague(stamp):
    """Convert an aware UTC timestamp to Europe/Prague wall time."""
    stamp = stamp.astimezone(timezone.utc)
    if _PRAGUE is not None:
        return stamp.astimezone(_PRAGUE)
    # EU rule: CEST from last Sunday of March 01:00 UTC to last Sunday of October 01:00 UTC.
    start = datetime.combine(_last_sunday(stamp.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(stamp.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    offset = 2 if start <= stamp < end else 1
    return stamp.astimezone(timezone(timedelta(hours=offset)))


def _parse(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None


def _dec(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _excluded_sell(sell, excluded_contracts, excluded_txs, bot_buys):
    symbol = str(sell.get("symbol") or "").upper()
    contract = str(sell.get("contract") or "").lower()
    source = str(sell.get("source") or "").upper()
    if symbol in EXCLUDED_SYMBOLS:
        return "EXTERNAL_ASSET"
    if contract and contract in excluded_contracts:
        return "EXTERNAL_ASSET"
    if str(sell.get("tx") or "").lower() in excluded_txs:
        return "EXTERNAL_UNMATCHED"
    if source in PAPER_SOURCES or sell.get("paper"):
        return "PAPER"
    buy_txs = sell.get("buy_tx")
    if isinstance(buy_txs, str):
        buy_txs = [buy_txs]
    if buy_txs and bot_buys and any(str(tx).lower() not in bot_buys for tx in buy_txs):
        return "NOT_BOT_BUY_BASIS"
    return None


def _period_keys(local):
    iso = local.isocalendar()
    return (local.date().isoformat(), f"{iso[0]}-W{iso[1]:02d}", f"{local.year}-{local.month:02d}")


def build_pnl_model(accounting, runtime_state=None, open_pnl=None, open_complete=False, now=None):
    """Build one realized/open/total model and its Prague day/week/month aggregations."""
    accounting = accounting if isinstance(accounting, dict) else {}
    runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    external = accounting.get("excluded_external_activity") or {}
    excluded_contracts = {str(x.get("contract") or "").lower()
                          for x in external.get("unknown_basis_assets") or []
                          if isinstance(x, dict) and x.get("contract")}
    excluded_txs = {str(x.get("sell_tx") or "").lower()
                    for x in external.get("unmatched_sells") or [] if isinstance(x, dict)}
    bot_buys = {str(x.get("tx") or "").lower()
                for x in accounting.get("buy_transactions") or [] if isinstance(x, dict) and x.get("tx")}

    events, excluded, seen = [], [], set()
    for sell in accounting.get("sell_transactions") or []:
        if not isinstance(sell, dict):
            continue
        tx = str(sell.get("tx") or "").lower()
        stamp = _parse(sell.get("at"))
        pnl = _dec(sell.get("net_pnl_sda"))
        reason = _excluded_sell(sell, excluded_contracts, excluded_txs, bot_buys)
        if reason is None and (not tx or stamp is None or pnl is None):
            reason = "INCOMPLETE_RECORD"
        if reason is None and tx in seen:
            reason = "DUPLICATE"
        if reason:
            excluded.append({"tx": sell.get("tx"), "symbol": sell.get("symbol"), "reason": reason})
            continue
        seen.add(tx)
        local = to_prague(stamp)
        day, week, month = _period_keys(local)
        events.append({"tx": sell.get("tx"), "at": sell.get("at"), "at_utc": stamp.isoformat(),
                       "local_date": day, "iso_week": week, "month": month,
                       "symbol": sell.get("symbol") or "UNKNOWN", "contract": sell.get("contract"),
                       "pnl": pnl, "proceeds": _dec(sell.get("proceeds_sda")),
                       "cost": _dec(sell.get("cost_basis_sda"))})
    events.sort(key=lambda e: (e["at_utc"], str(e["tx"])))

    # Only unresolved BOT trading evidence makes a period partial.
    partial_days, undated_unresolved = set(), []
    tx_times = {}
    for trade in runtime_state.get("trades") or []:
        if isinstance(trade, dict) and trade.get("tx"):
            tx_times[str(trade["tx"]).lower()] = _parse(trade.get("at"))
    for item in accounting.get("bot_unresolved_transactions") or []:
        stamp = None
        if isinstance(item, dict):
            stamp = _parse(item.get("at") or item.get("timestamp"))
            item = item.get("tx") or item.get("hash") or str(item)
        stamp = stamp or tx_times.get(str(item).lower())
        if stamp is None:
            undated_unresolved.append(str(item))
        else:
            partial_days.add(to_prague(stamp).date().isoformat())
    event_txs = {str(e["tx"]).lower() for e in events}
    pending_runtime = []
    for trade in runtime_state.get("trades") or []:
        if not (isinstance(trade, dict) and trade.get("action") == "SELL" and trade.get("sell_intent_id")):
            continue
        tx = str(trade.get("tx") or "").lower()
        stamp = _parse(trade.get("at"))
        if tx and tx not in event_txs and tx not in excluded_txs and stamp is not None:
            pending_runtime.append(trade.get("tx"))
            partial_days.add(to_prague(stamp).date().isoformat())

    def bucket(key):
        rows = {}
        for event in events:
            item = rows.setdefault(event[key], {"key": event[key], "pnl": Decimal(0), "sells": 0,
                                                "wins": 0, "losses": 0, "partial": False})
            item["pnl"] += event["pnl"]
            item["sells"] += 1
            item["wins"] += event["pnl"] > 0
            item["losses"] += event["pnl"] < 0
        return rows

    daily, weekly, monthly = bucket("local_date"), bucket("iso_week"), bucket("month")
    for day in partial_days:
        local = datetime.fromisoformat(day)
        _day, week, month = _period_keys(local)
        for rows, key in ((daily, _day), (weekly, week), (monthly, month)):
            rows.setdefault(key, {"key": key, "pnl": Decimal(0), "sells": 0, "wins": 0,
                                  "losses": 0, "partial": False})["partial"] = True
    # Every period exposes both its realized PnL delta and cumulative sum.
    # The cumulative sequence is built independently at each calendar granularity.
    for rows in (daily, weekly, monthly):
        cumulative = Decimal(0)
        for key in sorted(rows):
            item = rows[key]
            item["period_delta"] = item["pnl"]
            cumulative += item["pnl"]
            item["cumulative_sum"] = cumulative
            item["cumulative"] = cumulative

    realized = sum((e["pnl"] for e in events), Decimal(0))
    accounting_realized = _dec(accounting.get("realized_pnl_sda"))
    mismatch = accounting_realized is not None and abs(accounting_realized - realized) > Decimal("0.00000001")
    open_value = _dec(open_pnl) if open_pnl is not None else None
    local_now = to_prague(now)
    today, this_week, this_month = _period_keys(local_now)
    reconciliation_complete = str(accounting.get("bot_reconciliation_status") or "").upper() == "COMPLETE"
    realized_partial = (not reconciliation_complete) or bool(undated_unresolved) or bool(pending_runtime) or mismatch

    def period(rows, key):
        item = rows.get(key)
        delta = item["period_delta"] if item else Decimal(0)
        if item:
            cumulative_sum = item["cumulative_sum"]
        else:
            prior = [row for row_key, row in rows.items() if row_key < key]
            cumulative_sum = max(prior, key=lambda row: row["key"])["cumulative_sum"] if prior else Decimal(0)
        return {"pnl": delta, "period_delta": delta, "cumulative_sum": cumulative_sum,
                "partial": bool(item and item["partial"])}

    return {
        "events": events, "excluded": excluded,
        "realized": realized, "open": open_value,
        "total": realized + open_value if open_value is not None else None,
        "open_complete": bool(open_complete),
        "realized_partial": realized_partial,
        "accounting_realized": accounting_realized, "accounting_mismatch": mismatch,
        "undated_unresolved": undated_unresolved, "pending_runtime_sells": pending_runtime,
        "daily": daily, "weekly": weekly, "monthly": monthly,
        "today": period(daily, today), "this_week": period(weekly, this_week),
        "this_month": period(monthly, this_month),
        "today_key": today, "week_key": this_week, "month_key": this_month,
        "timezone": "Europe/Prague",
    }


def recent_days(model, days):
    """Prague calendar dates for the last ``days`` days ending today."""
    today = date.fromisoformat(model["today_key"])
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days)]


def recent_weeks(model, weeks):
    today = date.fromisoformat(model["today_key"])
    keys = []
    for offset in range(weeks):
        iso = (today - timedelta(weeks=offset)).isocalendar()
        keys.append(f"{iso[0]}-W{iso[1]:02d}")
    return keys
