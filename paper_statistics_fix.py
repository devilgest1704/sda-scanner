"""Canonical paper-trading statistics renderer.

Keeps realized P/L strictly ledger-based and calculates open P/L on the same
net-of-fee/slippage basis used by the paper close() execution.
"""
import telegram_dashboard as dashboard


def paper_statistics_report():
    p = dashboard.load("positions.json", {"positions": {}, "closed_trades": []})
    positions = p.get("positions", {}) or {}
    closed = p.get("closed_trades", []) or []
    if not isinstance(closed, list):
        closed = []

    market = dashboard.load("market_data.json", {"tokens": {}})
    tokens = market.get("tokens", {}) or {}
    meta = dashboard.load("token_metadata.json", {})
    engine = dashboard.engine
    fee = float(getattr(engine, "FEE_RATE", 0.01))
    slippage = float(getattr(engine, "SLIPPAGE_RATE", 0.001))

    # The ledger is the single source of truth for realized P/L. Never reuse a
    # cached cumulative/realized field from an older state file.
    realized = sum(engine.num(x.get("closed_profit_sda")) for x in closed if isinstance(x, dict))

    open_pnl = 0.0
    invested = 0.0
    open_rows = []
    for x in positions.values():
        if not isinstance(x, dict):
            continue
        frac = engine.num(x.get("remaining_fraction", 1))
        inv = engine.num(x.get("investment_sda"))
        active_inv = inv * frac
        invested += active_inv
        entry = engine.num(x.get("entry_price"))
        address = str(x.get("address", "")).lower()
        an = dashboard._analysis(tokens.get(address, {}) or {})
        cur = engine.num(an.get("price_in_sda"))

        # Mirror engine_legacy.close(): sell at current*(1-slippage), then
        # subtract the exit fee. Entry cost already includes the entry fee.
        if entry > 0 and cur > 0 and active_inv > 0:
            qty = active_inv / entry
            exit_value = qty * cur * (1.0 - slippage) * (1.0 - fee)
            entry_cost = active_inv * (1.0 + fee)
            pnl = exit_value - entry_cost
        else:
            pnl = 0.0
        open_pnl += pnl
        label = x.get("label") or engine.lbl(address, meta)
        open_rows.append((str(x.get("opened_at", "")), label, entry, cur, active_inv, pnl, engine.num(x.get("entry_confidence"))))

    cumulative = realized + open_pnl
    roi = cumulative / invested * 100.0 if invested else 0.0

    def pnl_text(value, unit="SDA"):
        value = engine.num(value)
        icon = "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")
        return f"{icon} {value:+.2f} {unit}"

    lines = [
        "📊 PAPER TRADING • STATISTICS",
        "",
        f"🟢 Open positions: {len(positions)}",
        f"📁 Closed trades: {len(closed)}",
        "────────────────────────",
        f"Realized P/L: {pnl_text(realized)}",
        f"Open P/L: {pnl_text(open_pnl)}",
        f"Cumulative P/L: {pnl_text(cumulative)}",
        f"ROI on open capital: {pnl_text(roi, '%')}",
        f"Open capital: {invested:.2f} SDA",
        f"🧮 Check: {realized:+.2f} + {open_pnl:+.2f} = {cumulative:+.2f} SDA",
    ]

    lines += ["", "📌 CURRENT POSITIONS", "────────────────────────"]
    if not open_rows:
        lines.append("⚪ No open paper positions")
    else:
        for _, label, entry, cur, inv, pnl, confidence in sorted(open_rows, reverse=True):
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            change = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0.0
            lines.append(f"{icon} {label}")
            lines.append(f"   Entry {entry:.6f} → {cur:.6f} SDA")
            lines.append(f"   P/L {pnl:+.2f} SDA ({change:+.2f}%) • {inv:.0f} SDA • score {confidence:.0f}")

    lines += ["", "📜 HISTORY", "────────────────────────"]
    if not closed:
        lines.append("⚪ No closed trades yet")
    else:
        for z in sorted(closed, key=lambda x: str(x.get("closed_at", "")), reverse=True)[:15]:
            label = z.get("label") or z.get("address", "UNKNOWN")
            profit = engine.num(z.get("closed_profit_sda"))
            roi_z = engine.num(z.get("closed_roi_pct"))
            reason = z.get("close_reason", "EXIT")
            icon = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪")
            lines.append(f"{icon} {label} • {reason}")
            lines.append(f"   {profit:+.2f} SDA ({roi_z:+.2f}%) • {str(z.get('closed_at',''))[:16].replace('T',' ')}")

    lines += ["", "────────────────────────", "Fee 1.0% • Slippage 0.1% • Paper only"]
    return "\n".join(lines)


def patch_dashboard(dashboard_module=None):
    target = dashboard_module or dashboard
    target.paper_statistics_report = paper_statistics_report
    return target
