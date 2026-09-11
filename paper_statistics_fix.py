"""Canonical paper-trading statistics renderer.

The Telegram UI can call either paper_report() or paper_statistics_report().
Both are patched here so there is one source of truth.
"""
import telegram_dashboard as dashboard


def _resolve_analysis(tokens, position, meta, engine):
    address = str(position.get("address") or "").strip().lower()
    candidates = []
    if address:
        candidates.extend([address, address.lower(), address.upper()])
    symbol = str(position.get("symbol") or "").strip().upper()
    label = str(position.get("label") or "").strip().upper()
    if symbol:
        candidates.append(symbol)
    if label:
        candidates.extend([label, label.split("/", 1)[0]])

    for key in candidates:
        if key in tokens and isinstance(tokens[key], dict):
            return dashboard._analysis(tokens[key] or {}), str(key)

    wanted = symbol or label.split("/", 1)[0]
    for key, td in tokens.items():
        if not isinstance(td, dict):
            continue
        raw = dashboard._analysis(td)
        td_symbol = str(raw.get("symbol") or td.get("symbol") or "").strip().upper()
        try:
            td_label = str(engine.lbl(key, meta) or "").strip().upper()
        except Exception:
            td_label = ""
        if wanted and (td_symbol == wanted or td_label == label or td_label.split("/", 1)[0] == wanted):
            return raw, str(key)
    return {}, address


def _closed_profit(x, engine):
    """Return the executed realized P/L recorded by the paper engine.

    `closed_profit_sda` is the canonical execution result. Do not reconstruct
    it from `closed_value_sda`: the latter is an output value and rebuilding
    the result here can apply entry fees a second time or otherwise diverge
    from the engine's actual close() accounting.
    """
    if not isinstance(x, dict):
        return 0.0
    if x.get("closed_profit_sda") is not None:
        return engine.num(x.get("closed_profit_sda"))
    return 0.0


def _closed_roi(x, engine):
    """Return the ROI recorded by the paper engine for the closed fraction."""
    if not isinstance(x, dict):
        return 0.0
    if x.get("closed_roi_pct") is not None:
        return engine.num(x.get("closed_roi_pct"))

    # Legacy fallback only for records without the canonical ROI field.
    profit = _closed_profit(x, engine)
    investment = engine.num(x.get("investment_sda"))
    fraction = engine.num(x.get("closed_fraction", x.get("remaining_fraction", 1)), 1.0)
    allocated = investment * fraction
    return profit / allocated * 100.0 if allocated else 0.0


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

    # Canonical realized P/L: exactly what the paper engine recorded at close.
    realized = sum(_closed_profit(x, engine) for x in closed)

    open_pnl = 0.0
    current_invested = 0.0
    historical_deployed = 0.0
    open_rows = []

    # Each closed fraction represents capital that was actually deployed.
    for x in closed:
        if not isinstance(x, dict):
            continue
        historical_deployed += engine.num(x.get("investment_sda")) * engine.num(x.get("closed_fraction", x.get("remaining_fraction", 1)))

    for x in positions.values():
        if not isinstance(x, dict):
            continue
        frac = engine.num(x.get("remaining_fraction", 1))
        inv = engine.num(x.get("investment_sda"))
        active_inv = inv * frac
        current_invested += active_inv
        historical_deployed += active_inv

        entry = engine.num(x.get("entry_price"))
        analysis, resolved_key = _resolve_analysis(tokens, x, meta, engine)
        cur = engine.num(analysis.get("price_in_sda"))

        # Mark-to-market using the same net exit economics as paper execution.
        if entry > 0 and cur > 0 and active_inv > 0:
            qty = active_inv / entry
            exit_value = qty * cur * (1.0 - slippage) * (1.0 - fee)
            entry_cost = active_inv * (1.0 + fee)
            pnl = exit_value - entry_cost
        else:
            pnl = 0.0

        open_pnl += pnl
        label = x.get("label") or engine.lbl(resolved_key, meta)
        confidence = engine.num(x.get("entry_confidence"))
        open_rows.append((str(x.get("opened_at", "")), label, entry, cur, active_inv, pnl, confidence))

    cumulative = realized + open_pnl
    roi = cumulative / historical_deployed * 100.0 if historical_deployed else 0.0

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
        f"ROI: {pnl_text(roi, '%')}",
        f"Current invested: {current_invested:.2f} SDA",
        f"Total deployed: {historical_deployed:.2f} SDA",
        f"🧮 Check: {realized:+.2f} + {open_pnl:+.2f} = {cumulative:+.2f} SDA",
    ]

    lines += ["", "📌 CURRENT POSITIONS", "────────────────────────"]
    if not open_rows:
        lines.append("⚪ No open paper positions")
    else:
        for _, label, entry, cur, inv, pnl, confidence in sorted(open_rows, reverse=True):
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            change = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0.0
            cur_txt = f"{cur:.6f}" if cur > 0 else "N/A"
            lines.append(f"{icon} {label}")
            lines.append(f"   Entry {entry:.6f} → {cur_txt} SDA")
            lines.append(f"   P/L {pnl:+.2f} SDA ({change:+.2f}%) • {inv:.0f} SDA • score {confidence:.0f}")

    lines += ["", "📜 HISTORY", "────────────────────────"]
    if not closed:
        lines.append("⚪ No closed trades yet")
    else:
        for z in sorted(closed, key=lambda x: str(x.get("closed_at", "")), reverse=True)[:15]:
            label = z.get("label") or z.get("address", "UNKNOWN")
            profit = _closed_profit(z, engine)
            roi_z = _closed_roi(z, engine)
            reason = z.get("close_reason", "EXIT")
            icon = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪")
            lines.append(f"{icon} {label} • {reason}")
            lines.append(f"   {profit:+.2f} SDA ({roi_z:+.2f}%) • {str(z.get('closed_at',''))[:16].replace('T',' ')}")

    lines += ["", "────────────────────────", "Fee 1.0% • Slippage 0.1% • Paper only"]
    return "\n".join(lines)


def paper_report():
    return paper_statistics_report()


def patch_dashboard(dashboard_module=None):
    target = dashboard_module or dashboard
    target.paper_statistics_report = paper_statistics_report
    target.paper_report = paper_report
    return target
