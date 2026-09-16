#!/usr/bin/env python3
import json
import os
import sys
import time
import requests

import engine
import main as scanner
from strategy_v21 import patch_engine
import v26_dashboard_patch

# Keep legacy engine compatibility, then let V26 own the dashboard presentation.
patch_engine(engine)

STATE_FILE = "telegram_menu_state.json"
REMOTE_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"


def load(path, default):
    if os.environ.get("SDA_REMOTE_STATE") == "1":
        try:
            r = requests.get(REMOTE_BASE + path, params={"ts": int(time.time())}, timeout=15)
            r.raise_for_status()
            value = r.json()
            return value if isinstance(value, type(default)) else default
        except Exception as exc:
            print(f"Remote state {path} error: {exc}")
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def api(method, payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN is not configured")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"Telegram {method} error: {exc}")
        return None


def send(text, reply_markup=None, chat_id=None):
    cid = chat_id or os.environ.get("CHAT_ID")
    if not cid:
        return
    payload = {"chat_id": cid, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    api("sendMessage", payload)


def edit(chat_id, message_id, text, reply_markup=None):
    if not chat_id or not message_id:
        return
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    api("editMessageText", payload)


def answer_callback(callback_id, text=None):
    if not callback_id:
        return
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    api("answerCallbackQuery", payload)


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🔄 Refresh", "callback_data": "REFRESH"}],
        [{"text": "📐 Technical Analysis", "callback_data": "TECH"}],
        [{"text": "🤖 Paper Trading", "callback_data": "PAPER"}],
        [{"text": "👛 Real Wallet & Positions", "callback_data": "REAL"}],
        [{"text": "📊 Market Momentum", "callback_data": "MOMENTUM"}],
        [{"text": "🐞 Market Debug", "callback_data": "DEBUG"}],
    ]}


def paper_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📊 Statistics", "callback_data": "PAPER_STATS"}],
        [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
    ]}


def real_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📈 Real Statistics", "callback_data": "REAL_STATS"}],
        [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
    ]}


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}


def _analysis(token_data):
    if not isinstance(token_data, dict):
        return {}
    value = token_data.get("analysis")
    return value if isinstance(value, dict) else token_data


def _pnl(value, unit="SDA"):
    try:
        n = float(value or 0)
    except Exception:
        n = 0.0
    icon = "🟢" if n > 0 else ("🔴" if n < 0 else "⚪")
    return f"{icon} {n:+.2f} {unit}"


def market_momentum_report():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    ranked = []
    for address, td in (md.get("tokens", {}) or {}).items():
        an = _analysis(td)
        if not an or engine.num(an.get("price_in_sda")) <= 0:
            continue
        try:
            d = engine.score(address, an, ws)
        except Exception:
            continue
        flow = an.get("flow", {}).get("1h", {}) or {}
        vol = engine.num(flow.get("total_volume"))
        if vol < 250:
            continue
        ranked.append((engine.num(d.get("confidence")), str(address), d, vol))
    ranked.sort(reverse=True, key=lambda x: (x[0], x[3]))
    lines = ["📊 MARKET MOMENTUM", "", "Active filter: 1H volume ≥ 250 SDA", "────────────────────────"]
    for score, address, d, vol in ranked[:10]:
        lines.append(f"{engine.lbl(address, meta)} • {score:.0f}/100")
        lines.append(f"   1H {engine.num(d.get('m1h')):+.2f}% • flow {engine.num(d.get('net_1h')):+.0f} SDA • vol {vol:.0f}")
    if len(lines) == 4:
        lines.append("⚪ No active tokens right now")
    return "\n".join(lines)


def technical_report():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    ranked = []
    for address, td in (md.get("tokens", {}) or {}).items():
        an = _analysis(td)
        if not an or engine.num(an.get("price_in_sda")) <= 0:
            continue
        try:
            d = engine.score(address, an, ws)
        except Exception:
            continue
        ranked.append((engine.num(d.get("confidence")), address, d))
    ranked.sort(reverse=True, key=lambda x: x[0])
    lines = ["📐 TECHNICAL ANALYSIS", "", "Read-only diagnostic", "────────────────────────"]
    for score, address, d in ranked[:5]:
        lines.append(f"{engine.lbl(address, meta)} • scanner {score:.0f}/100")
        lines.append(f"   15M {engine.num(d.get('m15')):+.2f}% • 1H {engine.num(d.get('m1h')):+.2f}% • 4H {engine.num(d.get('m4h')):+.2f}%")
    if len(lines) == 4:
        lines.append("⚪ No active candidates")
    return "\n".join(lines)


def paper_statistics_report():
    p = load("positions.json", {"positions": {}, "closed_trades": []})
    positions = p.get("positions", {}) or {}
    closed = p.get("closed_trades", []) or []
    market = load("market_data.json", {"tokens": {}})
    tokens = market.get("tokens", {}) or {}
    realized = sum(engine.num(x.get("closed_profit_sda")) for x in closed if isinstance(x, dict))
    open_pnl = 0.0
    invested = 0.0
    for x in positions.values():
        if not isinstance(x, dict):
            continue
        frac = engine.num(x.get("remaining_fraction", 1))
        inv = engine.num(x.get("investment_sda")) * frac
        invested += inv
        entry = engine.num(x.get("entry_price"))
        address = str(x.get("address", "")).lower()
        cur = engine.num(_analysis(tokens.get(address, {}) or {}).get("price_in_sda"))
        if entry > 0 and cur > 0:
            open_pnl += (cur / entry - 1) * inv
    total = realized + open_pnl
    roi = total / invested * 100 if invested else 0.0
    return "\n".join([
        "📊 PAPER TRADING • STATISTICS", "",
        f"🟢 Open positions: {len(positions)}",
        f"📁 Closed trades: {len(closed)}",
        "────────────────────────",
        f"Realized P/L: {_pnl(realized)}",
        f"Open P/L: {_pnl(open_pnl)}",
        f"Cumulative P/L: {_pnl(total)}",
        f"ROI: {_pnl(roi, '%')}",
        f"Invested: {invested:.2f} SDA",
        "", "👁 READ-ONLY • paper only",
    ])


def main_dashboard():
    try:
        return v26_dashboard_patch._top_buy(
            sys.modules[__name__],
            load("market_data.json", {"tokens": {}}),
            load("whale_data.json", {}),
            load("token_metadata.json", {}),
        )
    except Exception as exc:
        return f"📈 SDA MARKET SCANNER\n\n⚠️ Dashboard error: {exc}"


def _real_statistics_report():
    wallet = load("wallet_data.json", {})
    portfolio = load("portfolio_data.json", {})
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    realized = engine.num(portfolio.get("realized_pnl_sda")) if isinstance(portfolio, dict) else 0.0
    open_pnl = sum(engine.num(x.get("unrealized_pnl_sda")) for x in current.values() if isinstance(x, dict))
    return "\n".join([
        "📈 REAL TRADING • STATISTICS", "",
        f"🟢 Open positions: {len(current)}",
        f"Realized P/L: {realized:+.2f} SDA",
        f"Open P/L: {open_pnl:+.2f} SDA",
        f"Cumulative P/L: {realized + open_pnl:+.2f} SDA",
        f"Wallet: {engine.num(wallet.get('total_value_sda')):.2f} SDA",
        "", "👁 READ-ONLY",
    ])


def handle_update(update, state=None):
    state = state if state is not None else {"offset": 0}
    state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
    cb = update.get("callback_query") or {}
    data = str(cb.get("data") or "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    configured_chat = os.environ.get("CHAT_ID")
    if configured_chat and str(chat_id) != str(configured_chat):
        answer_callback(cb.get("id"), "Unauthorized")
        return state
    answer_callback(cb.get("id"))

    if data in ("REFRESH", "MAIN"):
        edit(chat_id, message_id, main_dashboard(), menu_keyboard())
    elif data == "PAPER":
        edit(chat_id, message_id, "🤖 PAPER TRADING\n\nVyber zobrazení:", paper_menu_keyboard())
    elif data == "PAPER_STATS":
        edit(chat_id, message_id, paper_statistics_report(), back_keyboard())
    elif data == "REAL":
        report = getattr(sys.modules[__name__], "real_trading_report", None)
        text = report() if callable(report) else "⚠️ Position Action unavailable"
        edit(chat_id, message_id, text, real_menu_keyboard())
    elif data == "REAL_STATS":
        edit(chat_id, message_id, _real_statistics_report(), back_keyboard())
    elif data == "TECH":
        edit(chat_id, message_id, technical_report(), back_keyboard())
    elif data == "MOMENTUM":
        edit(chat_id, message_id, market_momentum_report(), back_keyboard())
    elif data == "DEBUG":
        report = getattr(sys.modules[__name__], "market_debug_report", None)
        edit(chat_id, message_id, report() if callable(report) else "⚠️ Market Debug unavailable", back_keyboard())
    return state


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def listen_loop(duration_seconds=240):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN is not configured")
        return
    state = load(STATE_FILE, {"offset": 0})
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        result = api("getUpdates", {"offset": int(state.get("offset", 0)), "timeout": 20, "allowed_updates": ["callback_query"]})
        items = (result or {}).get("result", []) if isinstance(result, dict) else []
        for update in items:
            handle_update(update, state)
        save(STATE_FILE, state)


def run():
    if "--listen" in sys.argv or os.environ.get("TELEGRAM_MODE") == "listen":
        listen_loop()
        return
    send(main_dashboard(), menu_keyboard())


# Activate V26 presentation hooks AFTER all base dashboard functions exist.
v26_dashboard_patch.patch_dashboard(sys.modules[__name__])

# Preserve the existing read-only Real Wallet / Position Action integration.
try:
    import position_action_v20 as _position_action_v20
    _position_action_v20.patch_dashboard(sys.modules[__name__])
except Exception as exc:
    print(f"Position Action patch unavailable: {exc}")


if __name__ == "__main__":
    run()
