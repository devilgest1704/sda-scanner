"""Final Telegram callback router.

Installed after dashboard patches so every visible menu callback has one
explicit route, one prompt callback acknowledgement, and a safe edit fallback.
"""
import os

MAX_TELEGRAM_TEXT = 3900


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_callback_router_patched", False):
        return dashboard
    original_handle = dashboard.handle_update

    def _authorized(chat_id):
        configured = os.environ.get("CHAT_ID")
        return not configured or str(chat_id) == str(configured)

    def _safe_text(value, limit=MAX_TELEGRAM_TEXT):
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n\n… output truncated"

    def _send_fallback(chat_id, text, keyboard=None):
        if not chat_id:
            return False
        payload = {"chat_id": chat_id, "text": _safe_text(text)}
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            result = dashboard.api("sendMessage", payload)
            if isinstance(result, dict) and result.get("ok"):
                return True
            if keyboard:
                result = dashboard.api("sendMessage", {"chat_id": chat_id, "text": _safe_text(text)})
                return bool(isinstance(result, dict) and result.get("ok"))
        except Exception as exc:
            print(f"Telegram fallback send error: {type(exc).__name__}")
        return False

    def _safe_edit(chat_id, message_id, text, keyboard=None):
        if not chat_id or not message_id:
            return _send_fallback(chat_id, text, keyboard)
        try:
            result = dashboard.api(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id,
                 "text": _safe_text(text), **({"reply_markup": keyboard} if keyboard else {})},
            )
            if isinstance(result, dict) and result.get("ok"):
                return True
            description = str((result or {}).get("description") or "").lower()
            if "message is not modified" in description:
                return True
        except Exception as exc:
            print(f"Telegram edit fallback trigger: {type(exc).__name__}")
        return _send_fallback(chat_id, text, keyboard)

    def _real_bot_report(data):
        import real_bot_views
        return real_bot_views.render_view(
            dashboard, "main", refresh=(data == "REAL_BOT_REFRESH")
        )

    def handle_update(update, state=None):
        update = update or {}
        cb = update.get("callback_query") or {}
        data = str(cb.get("data") or "")
        handled = (
            "MAIN", "REAL", "REAL_STATS", "REFRESH", "DEBUG",
            "OPTIMIZER", "OPTIMIZER_STRATEGIES", "OPTIMIZER_V30",
            "OPT_CHAMPION", "OPT_CHALLENGER", "OPT_COMPARE", "OPT_RESEARCH", "OPT_ROLLBACK",
            "REAL_BOT", "REAL_BOT_REFRESH", "REAL_BOT_BACK",
            "REAL_BOT_PORTFOLIO", "REAL_BOT_PORTFOLIO_REFRESH",
            "REAL_BOT_BUY", "REAL_BOT_SELL",
            "REAL_BOT_STATS", "REAL_BOT_STATS_REFRESH",
            "REAL_BOT_SELL_V2", "REAL_BOT_SELL_V2_REFRESH",
            "REAL_BOT_PNL", "REAL_BOT_PNL_DAILY", "REAL_BOT_PNL_WEEKLY", "REAL_BOT_PNL_MONTHLY",
            "BUY_SCANNER", "BUY_SCANNER_DETAIL",
        )
        if data not in handled:
            return original_handle(update, state)

        state = state if state is not None else {"offset": 0}
        state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        if not _authorized(chat_id):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state

        # Acknowledge exactly once and before any GitHub, RPC, valuation or quote work.
        dashboard.answer_callback(cb.get("id"))

        real_bot_views = {
            "REAL_BOT": "main", "REAL_BOT_REFRESH": "main", "REAL_BOT_BACK": "main",
            "REAL_BOT_PORTFOLIO": "portfolio", "REAL_BOT_PORTFOLIO_REFRESH": "portfolio",
            "REAL_BOT_BUY": "buy", "REAL_BOT_SELL": "sell",
            "REAL_BOT_STATS": "stats", "REAL_BOT_STATS_REFRESH": "stats",
            "REAL_BOT_SELL_V2": "sell_v2", "REAL_BOT_SELL_V2_REFRESH": "sell_v2",
            "REAL_BOT_PNL": "pnl", "REAL_BOT_PNL_DAILY": "pnl_daily",
            "REAL_BOT_PNL_WEEKLY": "pnl_weekly", "REAL_BOT_PNL_MONTHLY": "pnl_monthly",
        }
        if data in real_bot_views:
            view = real_bot_views[data]
            refresh = data in {"REAL_BOT_REFRESH", "REAL_BOT_PORTFOLIO_REFRESH", "REAL_BOT_STATS_REFRESH", "REAL_BOT_SELL_V2_REFRESH"}
            cache_only = data in {"REAL_BOT_BACK", "REAL_BOT_PORTFOLIO", "REAL_BOT_BUY", "REAL_BOT_SELL", "REAL_BOT_STATS"}
            try:
                import real_bot_views as real_views
                if refresh or data == "REAL_BOT":
                    loading_keyboard = real_views._main_keyboard() if view == "main" else real_views._submenu_keyboard(view)
                    _safe_edit(chat_id, message_id, "🤖 REAL TRADING\n\n⏳ Updating verified portfolio data…", loading_keyboard)
                text, keyboard = real_views.render_view(dashboard, view, refresh=refresh, cache_only=cache_only)
            except Exception as exc:
                print(f"REAL Trading view error: {type(exc).__name__}: {exc}")
                text = "🤖 REAL TRADING\n\n⚠️ Data temporarily unavailable. Tap Refresh to retry."
                keyboard = {"inline_keyboard": [
                    [{"text": "🔄 Refresh", "callback_data": "REAL_BOT_REFRESH"}],
                    [{"text": "⬅️ REAL Menu", "callback_data": "REAL_BOT_BACK"}],
                ]}
            _safe_edit(chat_id, message_id, text, keyboard)
            return state

        if data in ("BUY_SCANNER", "BUY_SCANNER_DETAIL"):
            import telegram_home
            detail = data == "BUY_SCANNER_DETAIL"
            try:
                text = telegram_home.buy_scanner_text(dashboard, detail=detail)
            except Exception as exc:
                print(f"BUY Scanner error: {type(exc).__name__}: {exc}")
                text = "🔥 REAL BUY SCANNER\n\n⚠️ Scanner data temporarily unavailable."
            _safe_edit(chat_id, message_id, text, telegram_home.buy_scanner_keyboard(detail))
            return state

        optimizer_views = {
            "OPTIMIZER": "optimizer_overview_report",
            "OPTIMIZER_STRATEGIES": "optimizer_strategies_report",
            "OPTIMIZER_V30": "optimizer_research_report",
            "OPT_CHAMPION": "optimizer_champion_report",
            "OPT_CHALLENGER": "optimizer_challenger_report",
            "OPT_COMPARE": "optimizer_comparison_report",
            "OPT_RESEARCH": "optimizer_paper_research_report",
            "OPT_ROLLBACK": "optimizer_rollback_report",
        }
        if data in optimizer_views:
            try:
                report_fn = getattr(dashboard, optimizer_views[data], None)
                text = report_fn() if callable(report_fn) else "⚠️ Statistiky optimizeru nejsou dostupné."
            except Exception as exc:
                print(f"Optimizer menu error: {type(exc).__name__}: {exc}")
                text = "⚠️ Statistiky optimizeru nelze právě načíst."
            _safe_edit(chat_id, message_id, text, dashboard.optimizer_menu_keyboard())
            return state

        if data == "MAIN":
            try:
                text = dashboard.main_dashboard()
                keyboard = dashboard.menu_keyboard()
            except Exception as exc:
                print(f"Telegram main menu render error: {type(exc).__name__}: {exc}")
                text, keyboard = "⚠️ Main dashboard data unavailable. Menu navigation is still active.", dashboard.menu_keyboard()
            _safe_edit(chat_id, message_id, text, keyboard)
            return state

        if data == "REAL":
            try:
                import real_bot_views
                text,keyboard=real_bot_views.render_view(dashboard,"main",refresh=True)
            except Exception as exc:
                print(f"Telegram REAL menu render error: {type(exc).__name__}: {exc}")
                text="🤖 REAL TRADING\n\n⚠️ Blockchain accounting temporarily unavailable."
                keyboard=dashboard.menu_keyboard()
            _safe_edit(chat_id,message_id,text,keyboard)
            return state

        if data == "REAL_STATS":
            try:
                import real_bot_views
                text,keyboard=real_bot_views.render_view(dashboard,"stats",refresh=True)
            except Exception as exc:
                print(f"Telegram REAL statistics error: {type(exc).__name__}: {exc}")
                text="📈 REAL STATISTICS\n\n⚠️ Blockchain accounting temporarily unavailable."
                keyboard=dashboard.back_keyboard()
            _safe_edit(chat_id,message_id,text,keyboard)
            return state

        if data == "DEBUG":
            try:
                report_fn = getattr(dashboard, "market_debug_report", None)
                text = report_fn() if callable(report_fn) else "⚠️ Market Debug unavailable"
            except Exception as exc:
                text = f"🐞 MARKET DEBUG\n\n❌ Debug generation failed: {type(exc).__name__}: {exc}"
            _safe_edit(chat_id, message_id, text, dashboard.back_keyboard())
            return state

        # REFRESH re-renders the home screen; automatic/hourly dashboard pushes are disabled.
        try:
            import real_bot_views
            real_bot_views.clear_cache()
            text = dashboard.main_dashboard()
        except Exception as exc:
            text = "⚠️ Refresh failed. Main menu remains available."
            print(f"Telegram refresh error: {type(exc).__name__}: {exc}")
        _safe_edit(chat_id, message_id, text, dashboard.menu_keyboard())
        return state

    dashboard.handle_update = handle_update
    dashboard._sda_callback_router_patched = True
    return dashboard
