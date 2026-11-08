"""Final Telegram callback router.

Installed after dashboard patches so every visible menu callback has one
explicit route, one prompt callback acknowledgement, and a safe edit fallback.
"""
import json
import os
import urllib.error
import urllib.request

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

    def _dispatch_hourly_report():
        token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
        if not token:
            return False, "missing GITHUB_DISPATCH_TOKEN"
        url = "https://api.github.com/repos/devilgest1704/sda-scanner/actions/workflows/telegram_hourly.yml/dispatches"
        payload = json.dumps({"ref": "main", "inputs": {"run_reason": "Telegram Refresh"}}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                return response.status in (200, 201, 204), f"GitHub dispatch HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return False, f"GitHub dispatch HTTP {exc.code}: {body[:200]}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

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
            "REAL_BOT", "REAL_BOT_REFRESH", "REAL_BOT_BACK",
            "REAL_BOT_PORTFOLIO", "REAL_BOT_PORTFOLIO_REFRESH",
            "REAL_BOT_BUY", "REAL_BOT_SELL",
            "REAL_BOT_STATS", "REAL_BOT_STATS_REFRESH",
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
        }
        if data in real_bot_views:
            view = real_bot_views[data]
            refresh = data in {"REAL_BOT_REFRESH", "REAL_BOT_PORTFOLIO_REFRESH", "REAL_BOT_STATS_REFRESH"}
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

        if data in ("OPTIMIZER", "OPTIMIZER_STRATEGIES", "OPTIMIZER_V30"):
            try:
                name = {
                    "OPTIMIZER": "optimizer_overview_report",
                    "OPTIMIZER_STRATEGIES": "optimizer_strategies_report",
                    "OPTIMIZER_V30": "optimizer_research_report",
                }[data]
                report_fn = getattr(dashboard, name, None)
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
                report_fn = getattr(dashboard, "real_trading_report", None)
                text = report_fn() if callable(report_fn) else "⚠️ Real Wallet report unavailable"
            except Exception as exc:
                print(f"Telegram REAL menu render error: {type(exc).__name__}: {exc}")
                text = f"⚠️ REAL dashboard data unavailable ({type(exc).__name__})."
            keyboard = {"inline_keyboard": [
                [{"text": "📈 Real Statistics", "callback_data": "REAL_STATS"}],
                [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
            ]}
            _safe_edit(chat_id, message_id, text, keyboard)
            return state

        if data == "REAL_STATS":
            try:
                report_fn = getattr(dashboard, "real_statistics_report", None)
                text = report_fn() if callable(report_fn) else "⚠️ Real Statistics unavailable"
            except Exception as exc:
                print(f"Telegram REAL statistics error: {type(exc).__name__}: {exc}")
                text = "⚠️ REAL statistics unavailable. Use Back to return to the menu."
            _safe_edit(chat_id, message_id, text, dashboard.back_keyboard())
            return state

        if data == "DEBUG":
            try:
                report_fn = getattr(dashboard, "market_debug_report", None)
                text = report_fn() if callable(report_fn) else "⚠️ Market Debug unavailable"
            except Exception as exc:
                text = f"🐞 MARKET DEBUG\n\n❌ Debug generation failed: {type(exc).__name__}: {exc}"
            _safe_edit(chat_id, message_id, text, dashboard.back_keyboard())
            return state

        # REFRESH: callback was already acknowledged above. Never answer it twice.
        try:
            ok, result = _dispatch_hourly_report()
            text = dashboard.main_dashboard() + (
                "\n\n⏳ Telegram Hourly Report started manually…" if ok
                else f"\n\n❌ Hourly Report start failed: {result}"
            )
        except Exception as exc:
            text = "⚠️ Refresh failed. Main menu remains available."
            print(f"Telegram refresh error: {type(exc).__name__}: {exc}")
        _safe_edit(chat_id, message_id, text, dashboard.menu_keyboard())
        return state

    dashboard.handle_update = handle_update
    dashboard._sda_callback_router_patched = True
    return dashboard
