"""Position Action UI compatibility layer.

V26 is the single paper/decision policy. This module only wires the existing
Telegram Real Wallet menu to the V26 read-only reports.
"""
from copy import deepcopy
import v26_dashboard_patch


def _real_statistics_report(dashboard):
    wallet = dashboard.load("wallet_data.json", {})
    portfolio = deepcopy(dashboard.load("portfolio_data.json", {}))
    meta = dashboard.load("token_metadata.json", {})
    import main as scanner
    fifo = getattr(scanner, "_rebuild_fifo", None)
    if callable(fifo):
        try: portfolio = fifo(portfolio, meta)
        except Exception: pass
    trades = portfolio.get("trades", []) if isinstance(portfolio, dict) else []
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    sells = [x for x in trades if isinstance(x, dict) and str(x.get("side") or "").upper() == "SELL" and dashboard.engine.num(x.get("matched_amount")) > 0]
    profits = [dashboard.engine.num(x.get("matched_proceeds_sda")) - dashboard.engine.num(x.get("cost_basis_sda")) for x in sells]
    wins = [x for x in profits if x > 0]; losses = [x for x in profits if x < 0]
    realized = dashboard.engine.num(portfolio.get("realized_pnl_sda"))
    open_pnl = sum(dashboard.engine.num(x.get("unrealized_pnl_sda")) for x in current.values() if isinstance(x, dict))
    pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
    pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines = ["📈 REAL TRADING • STATISTICS", "", f"🟢 Open positions: {len(current)}", f"📁 Closed trades: {len(profits)}", "────────────────────────",
             f"Realized P/L: {realized:+.2f} SDA", f"Open P/L: {open_pnl:+.2f} SDA", f"Cumulative P/L: {realized+open_pnl:+.2f} SDA",
             f"Win rate: {(len(wins)/len(profits)*100 if profits else 0):.1f}%", f"Profit factor: {pf_text}", "", "👁 READ-ONLY"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_position_action_ui_patched", False): return dashboard
    v26_dashboard_patch.patch_dashboard(dashboard)
    original_handle_update = dashboard.handle_update
    original_menu_keyboard = dashboard.menu_keyboard

    def menu_keyboard_v26():
        keyboard = original_menu_keyboard()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REAL" for row in rows):
            rows.insert(2, [{"text": "👛 Real Wallet & Positions", "callback_data": "REAL"}])
        return keyboard

    def handle_update_v26(update, state=None):
        data = update if isinstance(update, dict) else {}
        callback = data.get("callback_query") or {}
        if callback:
            cb_data = str(callback.get("data") or "")
            callback_id = callback.get("id")
            msg = callback.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            message_id = msg.get("message_id")
            if cb_data == "REAL":
                dashboard.answer_callback(callback_id)
                report = getattr(dashboard, "real_trading_report", None)
                text = report() if callable(report) else "⚠️ Position Action unavailable"
                keyboard = {"inline_keyboard": [[{"text": "📈 Real Statistics", "callback_data": "REAL_STATS"}], [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}
                dashboard.edit(chat_id, message_id, text, keyboard)
                return state if state is not None else {"offset": 0}
            if cb_data == "REAL_STATS":
                dashboard.answer_callback(callback_id)
                dashboard.edit(chat_id, message_id, _real_statistics_report(dashboard), dashboard.back_keyboard())
                return state if state is not None else {"offset": 0}
        return original_handle_update(update, state)

    dashboard.menu_keyboard = menu_keyboard_v26
    dashboard.handle_update = handle_update_v26
    dashboard.real_statistics_report = lambda: _real_statistics_report(dashboard)
    dashboard._sda_position_action_ui_patched = True
    return dashboard
