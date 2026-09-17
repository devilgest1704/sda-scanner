import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner
import position_action_v20
import real_bot_menu
import paper_statistics_fix
import dashboard_consistency
import dashboard_v23_patch
import dashboard_wallet_summary
import real_wallet_market_fix
import v26_dashboard_patch


# V26 main-dashboard rendering calls this helper directly. Resolve wallet
# positions from the full market snapshot instead of relying on the current
# candidate subset. Keep this renderer independent from the dedicated
# Real Wallet portfolio renderer, which intentionally has no Position Action.
def _fixed_position_action_section(dashboard, md, ws, meta):
    try:
        full_md = real_wallet_market_fix._merge_market(dashboard)
        portfolio = dashboard.load("portfolio_data.json", {"current": {}})
        builder = v26_dashboard_patch._position_recommendations
        # _position_recommendations is a module-level helper whose first
        # argument is the dashboard instance. The previous call omitted it,
        # producing: missing 1 required positional argument: 'portfolio'.
        rows = builder(dashboard, full_md, ws, meta, portfolio) if callable(builder) else []
    except Exception as exc:
        print(f"Hourly Position Action error: {exc}")
        rows = []

    lines = ["", "🧭 POSITION ACTION • REAL WALLET", "────────────────────────"]
    if not rows:
        lines.append("⚪ No open real-wallet positions")
        return lines

    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "HOLD / WATCH")
        icon = {
            "EMERGENCY SELL": "🚨",
            "SELL / EXIT": "🔴",
            "PARTIAL SELL": "🟠",
            "HOLD / TRAIL": "🟢",
            "HOLD / PUMP": "🟢",
            "HOLD / WATCH": "🟡",
            "HOLD / NO COST BASIS": "🟡",
            "HOLD / MARKET DATA N/A": "🟡",
        }.get(action, "🟡")
        pnl_sda = row.get("pnl_sda")
        pnl = "UNKNOWN" if pnl_sda is None else f"{real_wallet_market_fix._n(pnl_sda):+.2f} SDA"
        score = "N/A" if row.get("score") is None else f"{real_wallet_market_fix._n(row.get('score')):.0f}/100"
        symbol = row.get("symbol") or row.get("token") or "UNKNOWN"
        lines.append(f"{icon} {symbol}: {action} • P/L {pnl} • score {score}")
        reason = str(row.get("reason") or "")
        if reason:
            lines.append(f"   {reason}")
    return lines


v26_dashboard_patch._position_action_section = _fixed_position_action_section


_original_dashboard_load = dashboard.load


def _dashboard_load_synced(path, default):
    data = _original_dashboard_load(path, default)
    if path == "portfolio_data.json" and isinstance(data, dict):
        wallet = _original_dashboard_load("wallet_data.json", {})
        return compact._sync_current_to_wallet(wallet, data)
    return data


def _install_dashboard_compat():
    """Provide the callback API expected by the dashboard patch modules.

    Telegram callbacks are handled by the Vercel webhook. The hourly GitHub
    Actions job only needs the dashboard callback API so the patch modules can
    wrap it; it must send one report and then exit.
    """
    if not hasattr(dashboard, "handle_update"):
        def handle_update(update, state=None):
            state = state if state is not None else {"offset": 0}
            cb = update.get("callback_query") or {}
            if cb:
                dashboard._handle_callback(cb)
                return state

            message = update.get("message") or {}
            text = str(message.get("text") or "").strip().lower()
            chat_id = (message.get("chat") or {}).get("id")
            configured_chat = dashboard.os.environ.get("CHAT_ID")
            if configured_chat and str(chat_id) != str(configured_chat):
                return state
            if text in ("/start", "/menu", "menu"):
                dashboard.send(dashboard.main_dashboard(), dashboard.menu_keyboard(), chat_id)
            return state

        dashboard.handle_update = handle_update


def _menu_without_unused_items():
    keyboard = dashboard.menu_keyboard()
    rows = []
    for row in keyboard.get("inline_keyboard", []):
        if not row:
            continue
        callback = row[0].get("callback_data")
        if callback in ("TECH", "MOMENTUM"):
            continue
        rows.append(row)
    keyboard["inline_keyboard"] = rows
    return keyboard


def main():
    _install_dashboard_compat()
    dashboard.load = _dashboard_load_synced
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    position_action_v20.patch_dashboard(dashboard)
    real_bot_menu.patch_dashboard(dashboard)
    paper_statistics_fix.patch_dashboard(dashboard)
    dashboard_consistency.patch_dashboard(dashboard)
    dashboard_v23_patch.patch_dashboard(dashboard)
    dashboard_wallet_summary.patch_dashboard(dashboard)

    # The GitHub Actions hourly report does not import api/index.py, so the
    # final Real Wallet resolver must be installed explicitly here too.
    # dashboard_consistency installs an older canonical renderer; this call
    # intentionally comes AFTER all dashboard patches.
    real_wallet_market_fix.patch_dashboard(dashboard)

    _patched_menu_keyboard = dashboard.menu_keyboard

    def _menu_keyboard_with_refresh():
        keyboard = _patched_menu_keyboard()
        rows = []
        for row in keyboard.get("inline_keyboard", []):
            if not row:
                continue
            callback = row[0].get("callback_data")
            if callback in ("TECH", "MOMENTUM"):
                continue
            rows.append(row)
        if not any(row and row[0].get("callback_data") == "REFRESH" for row in rows):
            rows.append([{"text": "🔄 Refresh", "callback_data": "REFRESH"}])
        keyboard["inline_keyboard"] = rows
        return keyboard

    dashboard.menu_keyboard = _menu_keyboard_with_refresh

    _original_handle_update = dashboard.handle_update

    def _handle_update_with_refresh(update, state=None):
        cb = update.get("callback_query") or {}
        if cb.get("data") != "REFRESH":
            return _original_handle_update(update, state)

        state = state if state is not None else {"offset": 0}
        state["offset"] = max(
            int(state.get("offset", 0)),
            int(update.get("update_id", 0)) + 1,
        )
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        configured_chat = dashboard.os.environ.get("CHAT_ID")

        if configured_chat and str(chat_id) != str(configured_chat):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state

        dashboard.answer_callback(cb.get("id"), "Refreshing…")
        dashboard.edit(
            chat_id,
            message_id,
            dashboard.main_dashboard(),
            dashboard.menu_keyboard(),
        )
        return state

    dashboard.handle_update = _handle_update_with_refresh

    # Some legacy compatibility layers still wrap the V26 main dashboard.
    # If one of those layers encounters malformed numeric state (e.g. None),
    # keep the hourly report alive and fall back to the canonical V26 renderer.
    _wrapped_main_dashboard = dashboard.main_dashboard

    def _safe_main_dashboard(*args, **kwargs):
        try:
            return _wrapped_main_dashboard(*args, **kwargs)
        except Exception as exc:
            print(f"Hourly dashboard wrapper error: {exc}")
            md = dashboard.load("market_data.json", {"tokens": {}})
            ws = dashboard.load("whale_data.json", {})
            meta = dashboard.load("token_metadata.json", {})
            return v26_dashboard_patch._top_buy(dashboard, md, ws, meta)

    dashboard.main_dashboard = _safe_main_dashboard

    dashboard.send(dashboard.main_dashboard(), dashboard.menu_keyboard())


if __name__ == "__main__": main()
