"""Final Telegram callback router.

Installed after all dashboard UI patches so MAIN/REAL/REAL_STATS/REFRESH/DEBUG
callbacks cannot be swallowed by nested compatibility wrappers.
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
        if len(text) <= limit:return text
        return text[:limit].rstrip() + "\n\n… output truncated"
    def _send_fallback(chat_id, text, keyboard=None):
        if not chat_id:return
        payload={"chat_id":chat_id,"text":_safe_text(text)}
        if keyboard:payload["reply_markup"]=keyboard
        try:dashboard.api("sendMessage",payload)
        except Exception as exc:print(f"Telegram fallback send error: {exc}")
    def _safe_edit(chat_id,message_id,text,keyboard=None):
        if not chat_id or not message_id:return False
        text=_safe_text(text);payload={"chat_id":chat_id,"message_id":message_id,"text":text}
        if keyboard:payload["reply_markup"]=keyboard
        try:
            result=dashboard.api("editMessageText",payload)
            if isinstance(result,dict) and result.get("ok"):return True
        except Exception as exc:print(f"Telegram edit fallback trigger: {exc}")
        _send_fallback(chat_id,text,keyboard);return False
    def _dispatch_hourly_report():
        token=os.environ.get("GITHUB_DISPATCH_TOKEN","")
        if not token:return False,"missing GITHUB_DISPATCH_TOKEN"
        url="https://api.github.com/repos/devilgest1704/sda-scanner/actions/workflows/telegram_hourly.yml/dispatches"
        payload=json.dumps({"ref":"main","inputs":{"run_reason":"Telegram Refresh"}}).encode("utf-8")
        req=urllib.request.Request(url,data=payload,headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28","Content-Type":"application/json"},method="POST")
        try:
            with urllib.request.urlopen(req,timeout=20) as response:return response.status in (200,201,204),f"GitHub dispatch HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            try:body=exc.read().decode("utf-8",errors="replace")
            except Exception:body=""
            return False,f"GitHub dispatch HTTP {exc.code}: {body[:300]}"
        except Exception as exc:return False,str(exc)
    def handle_update(update,state=None):
        cb=(update or {}).get("callback_query") or {};data=str(cb.get("data") or "")
        if data not in ("MAIN","REAL","REAL_STATS","REFRESH","DEBUG"):
            return original_handle(update,state)
        state=state if state is not None else {"offset":0};state["offset"]=max(int(state.get("offset",0)),int((update or {}).get("update_id",0))+1)
        msg=cb.get("message") or {};chat_id=(msg.get("chat") or {}).get("id");message_id=msg.get("message_id")
        if not _authorized(chat_id):dashboard.answer_callback(cb.get("id"),"Unauthorized");return state
        dashboard.answer_callback(cb.get("id"))
        if data=="MAIN":
            try:text=dashboard.main_dashboard();keyboard=dashboard.menu_keyboard();_safe_edit(chat_id,message_id,text,keyboard)
            except Exception as exc:_safe_edit(chat_id,message_id,f"⚠️ Main dashboard error: {type(exc).__name__}: {exc}",dashboard.menu_keyboard())
            return state
        if data=="REAL":
            try:
                report_fn=getattr(dashboard,"real_trading_report",None);text=report_fn() if callable(report_fn) else "⚠️ Real Wallet report unavailable"
            except Exception as exc:text=f"⚠️ Real Wallet error: {type(exc).__name__}: {exc}"
            keyboard={"inline_keyboard":[[{"text":"📈 Real Statistics","callback_data":"REAL_STATS"}],[{"text":"⬅️ Main dashboard","callback_data":"MAIN"}]]};_safe_edit(chat_id,message_id,text,keyboard);return state
        if data=="REAL_STATS":
            try:
                report_fn=getattr(dashboard,"real_statistics_report",None);text=report_fn() if callable(report_fn) else "⚠️ Real Statistics unavailable"
            except Exception as exc:text=f"⚠️ Real Statistics error: {type(exc).__name__}: {exc}"
            _safe_edit(chat_id,message_id,text,dashboard.back_keyboard());return state
        if data=="DEBUG":
            try:
                report_fn=getattr(dashboard,"market_debug_report",None)
                text=report_fn() if callable(report_fn) else "⚠️ Market Debug unavailable"
            except Exception as exc:
                text=f"🐞 MARKET DEBUG\n\n❌ Debug generation failed: {type(exc).__name__}: {exc}"
            _safe_edit(chat_id,message_id,text,dashboard.back_keyboard());return state
        dashboard.answer_callback(cb.get("id"),"Report started…")
        try:
            ok,result=_dispatch_hourly_report()
            text=dashboard.main_dashboard()+("\n\n⏳ Telegram Hourly Report started manually…" if ok else f"\n\n❌ Hourly Report start failed: {result}")
        except Exception as exc:text=dashboard.main_dashboard()+f"\n\n❌ Refresh error: {type(exc).__name__}: {exc}"
        _safe_edit(chat_id,message_id,text,dashboard.menu_keyboard());return state
    dashboard.handle_update=handle_update
    dashboard._sda_callback_router_patched=True
    return dashboard
