"""Final Telegram callback router.

Installed after all dashboard UI patches so MAIN/REAL callbacks cannot be
swallowed by nested compatibility wrappers.
"""
import os


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_callback_router_patched", False):
        return dashboard

    original_handle = dashboard.handle_update

    def _authorized(chat_id):
        configured = os.environ.get("CHAT_ID")
        return not configured or str(chat_id) == str(configured)

    def handle_update(update, state=None):
        cb = (update or {}).get("callback_query") or {}
        data = str(cb.get("data") or "")
        if data not in ("MAIN", "REAL", "REAL_STATS"):
            return original_handle(update, state)

        state = state if state is not None else {"offset": 0}
        state["offset"] = max(int(state.get("offset", 0)), int((update or {}).get("update_id", 0)) + 1)
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")

        if not _authorized(chat_id):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state

        dashboard.answer_callback(cb.get("id"))

        if data == "MAIN":
            dashboard.edit(
                chat_id,
                message_id,
                dashboard.main_dashboard(),
                dashboard.menu_keyboard(),
            )
            return state

        if data == "REAL":
            report_fn = getattr(dashboard, "real_trading_report", None)
            text = report_fn() if callable(report_fn) else "⚠️ Real Wallet report unavailable"
            dashboard.edit(
                chat_id,
                message_id,
                text,
                {"inline_keyboard": [
                    [{"text": "📈 Real Statistics", "callback_data": "REAL_STATS"}],
                    [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
                ]},
            )
            return state

        # REAL_STATS is owned by position_action_v20 and exposed on dashboard.
        report_fn = getattr(dashboard, "real_statistics_report", None)
        if callable(report_fn):
            text = report_fn()
        else:
            text = "⚠️ Real Statistics unavailable"
        dashboard.edit(chat_id, message_id, text, dashboard.back_keyboard())
        return state

    dashboard.handle_update = handle_update
    dashboard._sda_callback_router_patched = True
    return dashboard
