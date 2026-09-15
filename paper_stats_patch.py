"""Paper-trading accounting patch.

Partial exits are execution events, not separate completed trades. This layer
keeps the economic P/L unchanged while making closed-trade statistics count
one completed position once.
"""
from __future__ import annotations


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _trade_key(row):
    if not isinstance(row, dict):
        return None
    return (str(row.get("address") or "").lower(), str(row.get("opened_at") or ""))


def _migrate(p):
    """Collapse legacy PARTIAL CLOSED rows into their final CLOSED row.

    Existing total realized P/L is preserved exactly by summing all exit P/L
    into the completed trade. If a partial-only position has no final close,
    it remains in partial_exits and is not counted as a completed trade.
    """
    if not isinstance(p, dict):
        return
    rows = p.setdefault("closed_trades", [])
    partial_store = p.setdefault("partial_exits", [])
    if not isinstance(rows, list):
        rows = []
        p["closed_trades"] = rows

    groups = {}
    leftovers = []
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            leftovers.append(row)
            continue
        key = _trade_key(row)
        if key is None:
            leftovers.append(row)
            continue
        groups.setdefault(key, []).append(row)

    new_closed = []
    for key, group in groups.items():
        finals = [r for r in group if str(r.get("status", "")).upper() == "CLOSED"]
        partials = [r for r in group if str(r.get("status", "")).upper() == "PARTIAL CLOSED"]
        if finals and partials:
            final = dict(finals[-1])
            partial_profit = sum(_num(r.get("closed_profit_sda")) for r in partials)
            partial_value = sum(_num(r.get("closed_value_sda")) for r in partials)
            final["closed_profit_sda"] = round(_num(final.get("closed_profit_sda")) + partial_profit, 6)
            final["closed_value_sda"] = round(_num(final.get("closed_value_sda")) + partial_value, 6)
            final["realized_from_partial_exits_sda"] = round(partial_profit, 6)
            final["partial_exit_count"] = len(partials)
            final["status"] = "CLOSED"
            cost = _num(final.get("investment_sda"))
            final["closed_roi_pct"] = (final["closed_profit_sda"] / (cost * 1.01) * 100) if cost else 0.0
            new_closed.append(final)
            partial_store.extend(partials)
            changed = True
        else:
            new_closed.extend(group)

    new_closed.extend(leftovers)
    if changed:
        p["closed_trades"] = new_closed[-500:]
        p["partial_exits"] = partial_store[-1000:]
        p["stats_migrated"] = True


def _stats(p):
    rows = p.get("closed_trades", []) if isinstance(p, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    wins = sum(1 for r in rows if _num(r.get("closed_profit_sda")) > 0)
    pnl = sum(_num(r.get("closed_profit_sda")) for r in rows)
    gross_win = sum(_num(r.get("closed_profit_sda")) for r in rows if _num(r.get("closed_profit_sda")) > 0)
    gross_loss = sum(_num(r.get("closed_profit_sda")) for r in rows if _num(r.get("closed_profit_sda")) < 0)
    losses = len(rows) - wins
    avg = pnl / len(rows) if rows else 0.0
    avg_win = gross_win / wins if wins else 0.0
    avg_loss = gross_loss / losses if losses else 0.0
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(rows) * 100, 2) if rows else 0.0,
        "realized_pnl_sda": round(pnl, 6),
        "avg_pnl_per_trade_sda": round(avg, 6),
        "avg_win_sda": round(avg_win, 6),
        "avg_loss_sda": round(avg_loss, 6),
        "profit_factor": round(gross_win / abs(gross_loss), 4) if gross_loss else None,
    }


def patch(engine):
    if getattr(engine, "_paper_stats_patch", False):
        return engine

    original_load = engine.load
    original_save = engine.save
    original_close = engine.close

    # Migrate legacy ledgers immediately when loaded by the engine.
    def load(path, default):
        data = original_load(path, default)
        if path == engine.POSITIONS_FILE and isinstance(data, dict):
            _migrate(data)
        return data

    def close(p, address, current, reason, fraction=None):
        x = p.get("positions", {}).get(address)
        if not x:
            return None
        remaining = _num(x.get("remaining_fraction"), 1.0)
        frac = remaining if fraction is None else min(remaining, max(0.0, _num(fraction)))
        if frac <= 0:
            return None
        z = original_close(p, address, current, reason, fraction)
        if not z:
            return z
        # original_close appended partial rows. Move them out of the completed
        # trade ledger; retain them as execution events and accumulate their P/L
        # on the remaining position for the final close.
        if str(z.get("status", "")).upper() == "PARTIAL CLOSED":
            partials = p.setdefault("partial_exits", [])
            partials.append(z)
            p["closed_trades"] = [r for r in p.get("closed_trades", []) if r is not z and not (
                isinstance(r, dict) and r.get("closed_at") == z.get("closed_at") and
                r.get("address") == z.get("address") and r.get("status") == "PARTIAL CLOSED"
            )]
            pos = p.get("positions", {}).get(address)
            if isinstance(pos, dict):
                pos["realized_partial_profit_sda"] = round(
                    _num(pos.get("realized_partial_profit_sda")) + _num(z.get("closed_profit_sda")), 6
                )
                pos["partial_exit_count"] = int(_num(pos.get("partial_exit_count"))) + 1
        else:
            partial_profit = _num(z.get("realized_partial_profit_sda"))
            if partial_profit:
                z["realized_from_partial_exits_sda"] = round(partial_profit, 6)
                z["partial_exit_count"] = int(_num(x.get("partial_exit_count")))
                z["closed_profit_sda"] = round(_num(z.get("closed_profit_sda")) + partial_profit, 6)
                cost = _num(z.get("investment_sda")) * 1.01
                z["closed_roi_pct"] = z["closed_profit_sda"] / cost * 100 if cost else 0.0
                # Replace the just-appended final row with the aggregated row.
                if p.get("closed_trades"):
                    p["closed_trades"][-1] = z
        return z

    def save(path, data):
        if path == engine.POSITIONS_FILE and isinstance(data, dict):
            _migrate(data)
        return original_save(path, data)

    engine.load = load
    engine.save = save
    engine.close = close
    engine._paper_trade_stats = _stats
    engine._paper_stats_patch = True
    return engine
