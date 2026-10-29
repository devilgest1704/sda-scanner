"""Rolling 24-hour loss circuit for new PAPER orders only.

An observed, current paper ledger is the only source of P/L. This module
does not change stops, close positions or touch real trading. No speculative
claims about strategy improvement are made.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import groupby
from pathlib import Path
import json
import math

WINDOW_HOURS = 24
MIN_CLOSED_TRADES = 8
LOSS_BUDGET_SDA = 20.0
CURRENT_LEDGER = "positions.json"


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _pnl(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) else None


@lru_cache(maxsize=4)
def _read_ledger(path, minute_bucket):
    # Cache only within a scan-minute; do not hold a stale guard indefinitely
    # in long-running dashboards or service instances.
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("closed_trades"), list):
            return None
        return data["closed_trades"]
    except (OSError, ValueError, TypeError):
        return None


def evaluate_closed(rows, at):
    """Return a transparent gate based ONLY on completed observed trades."""
    base = {
        "paper_only": True, "window_hours": WINDOW_HOURS,
        "minimum_closed": MIN_CLOSED_TRADES, "loss_budget_sda": LOSS_BUDGET_SDA,
        "blocked": False, "status": "COLLECTING", "reason": "insufficient_recent_closes",
        "window_closed": 0, "window_wins": 0, "window_pnl_sda": 0.0,
        "last_closed_at": None,
        "projected_recovery_at_no_new_closes": None,
    }
    if not isinstance(rows, list) or not isinstance(at, datetime) or at.tzinfo is None:
        return {**base, "blocked": True, "status": "INVALID_LEDGER",
                "reason": "missing_or_invalid_current_paper_ledger"}
    now = at.astimezone(timezone.utc)
    floor = now - timedelta(hours=WINDOW_HOURS)
    recent = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "CLOSED":
            continue
        closed = _when(row.get("closed_at"))
        if closed is None or closed > now or closed < floor:
            continue
        pnl = _pnl(row.get("closed_profit_sda"))
        if pnl is None:
            # Unsafe accounting: never issue a new simulated buy on invalid
            # current-window P/L, but do not interrupt existing exits.
            return {**base, "blocked": True, "status": "INVALID_LEDGER",
                    "reason": "nonfinite_recent_closed_trade_pnl"}
        recent.append((closed, pnl))
    total = sum(pnl for _, pnl in recent)
    count = len(recent)
    last = max((stamp for stamp, _ in recent), default=None)
    # A capital loss budget is absolute; sample sufficiency is diagnostic.
    # Seven losses at -25 SDA must not admit an eighth new PAPER entry.
    guard = total <= -LOSS_BUDGET_SDA
    # Deterministic projection from the ORIGINAL ledger, NOT a relaxation
    # of the 24h risk budget. A new close can move this time.
    recovery = None
    if guard:
        remaining_count, remaining_pnl = count, total
        for closed_at, rows_at_time in groupby(sorted(recent), key=lambda x: x[0]):
            expired = list(rows_at_time)
            remaining_count -= len(expired)
            remaining_pnl -= sum(pnl for _, pnl in expired)
            if remaining_pnl > -LOSS_BUDGET_SDA:
                # Rows exactly 24 hours old are still included: use the
                # first instant STRICTLY after their 24h expiry.
                recovery = (closed_at + timedelta(
                    hours=WINDOW_HOURS, microseconds=1)).isoformat()
                break
    return {
        **base,
        "blocked": guard,
        "status": "LOSS_COOLDOWN" if guard else ("ACTIVE" if count >= MIN_CLOSED_TRADES else "COLLECTING"),
        "reason": "24h_realized_loss_budget_exceeded" if guard else (
            "within_observed_loss_budget" if count >= MIN_CLOSED_TRADES else "insufficient_recent_closes"),
        "window_closed": count,
        "window_wins": sum(pnl > 0 for _, pnl in recent),
        "window_pnl_sda": round(total, 4),
        "last_closed_at": last.isoformat() if last else None,
        "projected_recovery_at_no_new_closes": recovery,
    }


def paper_entry_risk_gate(now=None, ledger_path=CURRENT_LEDGER):
    when = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(when, datetime) or when.tzinfo is None:
        return evaluate_closed(None, when)
    minute_bucket = int(when.timestamp() // 60)
    rows = _read_ledger(str(ledger_path), minute_bucket)
    return evaluate_closed(rows, when)


def main():
    print("PAPER 24H LOSS CIRCUIT:", json.dumps(paper_entry_risk_gate(),
          ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
