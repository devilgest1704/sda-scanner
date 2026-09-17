#!/usr/bin/env python3
import json
import hashlib
import os
import sys
import time
import requests

import engine
import main as scanner
from strategy_v21 import patch_engine
import v26_dashboard_patch

patch_engine(engine)
STATE_FILE = "telegram_menu_state.json"
REMOTE_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"

def load(path, default):
    if os.environ.get("SDA_REMOTE_STATE") == "1":
        try:
            r=requests.get(REMOTE_BASE+path,params={"ts":int(time.time())},timeout=15);r.raise_for_status();value=r.json()
            return value if isinstance(value,type(default)) else default
        except Exception as exc: print(f"Remote state {path} error: {exc}")
    try:
        with open(path,encoding="utf-8") as f:value=json.load(f)
        return value if isinstance(value,type(default)) else default
    except Exception:return default

def _snapshot_id(md,ws=None,meta=None):
    raw=json.dumps({"market":md if isinstance(md,dict) else {},"whale":ws if isinstance(ws,dict) else {},"meta":meta if isinstance(meta,dict) else {}},ensure_ascii=False,sort_keys=True,default=str,separators=(",",":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

def _snapshot_time(md,ws=None):
    for src in (md,ws):
        if isinstance(src,dict):
            for key in ("updated_at","generated_at","timestamp","last_updated","time"):
                if src.get(key) not in (None,""):return str(src[key])
    return "unknown"

def api(method,payload):
    token=os.environ.get("TELEGRAM_TOKEN")
    if not token:return None
    try:
        r=requests.post(f"https://api.telegram.org/bot{token}/{method}",json=payload,timeout=30);r.raise_for_status();return r.json()
    except Exception as exc:print(f"Telegram {method} error: {exc}");return None

def send(text,reply_markup=None,chat_id=None):
    cid=chat_id or os.environ.get("CHAT_ID")
    if cid:
        payload={"chat_id":cid,"text":text}
        if reply_markup:payload["reply_markup"]=reply_markup
        api("sendMessage",payload)

def edit(chat_id,message_id,text,reply_markup=None):
    if chat_id and message_id:
        payload={"chat_id":chat_id,"message_id":message_id,"text":text}
        if reply_markup:payload["reply_markup"]=reply_markup
        api("editMessageText",payload)

def answer_callback(callback_id,text=None):
    if callback_id:
        payload={"callback_query_id":callback_id}
        if text:payload["text"]=text
        api("answerCallbackQuery",payload)

def menu_keyboard():return {"inline_keyboard":[[{"text":"🔄 Refresh","callback_data":"REFRESH"}],[{"text":"📐 Technical Analysis","callback_data":"TECH"}],[{"text":"🤖 Paper Trading","callback_data":"PAPER"}],[{"text":"👛 Real Wallet & Positions","callback_data":"REAL"}],[{"text":"📊 Market Momentum","callback_data":"MOMENTUM"}],[{"text":"🐞 Market Debug","callback_data":"DEBUG"}]]}
def paper_menu_keyboard():return {"inline_keyboard":[[{"text":"📊 Statistics","callback_data":"PAPER_STATS"}],[{"text":"⬅️ Main dashboard","callback_data":"MAIN"}]]}
def real_menu_keyboard():return {"inline_keyboard":[[{"text":"📈 Real Statistics","callback_data":"REAL_STATS"}],[{"text":"⬅️ Main dashboard","callback_data":"MAIN"}]]}
def back_keyboard():return {"inline_keyboard":[[{"text":"⬅️ Main dashboard","callback_data":"MAIN"}]]}

def _analysis(td):
    if not isinstance(td,dict):return {}
    x=td.get("analysis");return x if isinstance(x,dict) else td

def market_momentum_report():return "📊 MARKET MOMENTUM\n\nRead-only diagnostic"
def technical_report():return "📐 TECHNICAL ANALYSIS\n\nRead-only diagnostic"
def paper_statistics_report():return "📊 PAPER TRADING • STATISTICS\n\nRead-only"

def main_dashboard():
    try:
        md=load("market_data.json",{"tokens":{}});ws=load("whale_data.json",{});meta=load("token_metadata.json",{})
        text=v26_dashboard_patch._top_buy(sys.modules[__name__],md,ws,meta)
        if "⚪ No open real-wallet positions" in text:
            rw=getattr(sys.modules[__name__],"real_wallet_position_action",None)
            if callable(rw):
                old="\n🧭 POSITION ACTION • REAL WALLET\n────────────────────────\n⚪ No open real-wallet positions"
                text=text.replace(old,"\n"+"\n".join(rw()))
        return text
    except Exception as exc:return f"📈 SDA MARKET SCANNER\n\n⚠️ Dashboard error: {exc}"

def _real_statistics_report():
    report=getattr(sys.modules[__name__],"real_trading_report",None)
    return report() if callable(report) else "⚠️ Real Wallet unavailable"

def handle_update(update,state=None):
    state=state if state is not None else {"offset":0};state["offset"]=max(int(state.get("offset",0)),int(update.get("update_id",0))+1)
    cb=update.get("callback_query") or {};data=str(cb.get("data") or "");msg=cb.get("message") or {};chat_id=(msg.get("chat") or {}).get("id");message_id=msg.get("message_id")
    configured=os.environ.get("CHAT_ID")
    if configured and str(chat_id)!=str(configured):answer_callback(cb.get("id"),"Unauthorized");return state
    answer_callback(cb.get("id"))
    if data in ("REFRESH","MAIN"):edit(chat_id,message_id,main_dashboard(),menu_keyboard())
    elif data=="PAPER":edit(chat_id,message_id,"🤖 PAPER TRADING\n\nVyber zobrazení:",paper_menu_keyboard())
    elif data=="PAPER_STATS":edit(chat_id,message_id,paper_statistics_report(),back_keyboard())
    elif data=="REAL":edit(chat_id,message_id,_real_statistics_report(),real_menu_keyboard())
    elif data=="REAL_STATS":edit(chat_id,message_id,_real_statistics_report(),back_keyboard())
    elif data=="TECH":edit(chat_id,message_id,technical_report(),back_keyboard())
    elif data=="MOMENTUM":edit(chat_id,message_id,market_momentum_report(),back_keyboard())
    elif data=="DEBUG":
        report=getattr(sys.modules[__name__],"market_debug_report",None);text=report() if callable(report) else "⚠️ Market Debug unavailable";edit(chat_id,message_id,text,back_keyboard())
    return state

def save(path,data):
    with open(path,"w",encoding="utf-8") as f:json.dump(data,f,ensure_ascii=False,indent=2)

def listen_loop(duration_seconds=240):
    token=os.environ.get("TELEGRAM_TOKEN")
    if not token:return
    state=load(STATE_FILE,{"offset":0});deadline=time.monotonic()+duration_seconds
    while time.monotonic()<deadline:
        result=api("getUpdates",{"offset":int(state.get("offset",0)),"timeout":20,"allowed_updates":["callback_query"]})
        for update in ((result or {}).get("result",[]) if isinstance(result,dict) else []):handle_update(update,state)
        save(STATE_FILE,state)

def run():
    if "--listen" in sys.argv or os.environ.get("TELEGRAM_MODE")=="listen":listen_loop();return
    send(main_dashboard(),menu_keyboard())

v26_dashboard_patch.patch_dashboard(sys.modules[__name__])
try:
    import position_action_v20 as _position_action_v20
    _position_action_v20.patch_dashboard(sys.modules[__name__])
except Exception as exc:print(f"Position Action patch unavailable: {exc}")
try:
    import real_wallet_market_fix as _rw
    _rw.patch_dashboard(sys.modules[__name__])
except Exception as exc:print(f"Real Wallet patch unavailable: {exc}")
try:
    import dashboard_runtime_guard as _runtime_guard
    _runtime_guard.patch_dashboard(sys.modules[__name__])
except Exception as exc:print(f"Dashboard runtime guard unavailable: {exc}")

if __name__=="__main__":run()
