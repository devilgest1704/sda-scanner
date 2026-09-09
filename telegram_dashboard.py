#!/usr/bin/env python3
import json
import os
import sys
import time
import requests

import engine
import main as scanner

STATE_FILE = "telegram_menu_state.json"
REMOTE_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"


def load(path, default):
    if os.environ.get("SDA_REMOTE_STATE") == "1":
        try:
            r = requests.get(REMOTE_BASE + path, params={"ts": int(time.time())}, timeout=15)
            r.raise_for_status()
            x = r.json()
            return x if isinstance(x, type(default)) else default
        except Exception as exc:
            print(f"Remote state {path} error: {exc}")
    try:
        with open(path, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, type(default)) else default
    except Exception:
        return default


def api(method, payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
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
    if callback_id:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        api("answerCallbackQuery", payload)


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📐 Technical Analysis", "callback_data": "TECH"}],
        [{"text": "🤖 Paper Trading", "callback_data": "PAPER"}],
        [{"text": "📊 Market Momentum", "callback_data": "MOMENTUM"}],
        [{"text": "🐞 Market Debug", "callback_data": "DEBUG"}],
    ]}


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}


def _analysis(token_data):
    if not isinstance(token_data, dict):
        return {}
    value = token_data.get("analysis")
    return value if isinstance(value, dict) else token_data


def _ranked(md, ws):
    ranked = []
    tokens = md.get("tokens", {}) or {}
    for address, token_data in tokens.items():
        analysis = _analysis(token_data)
        if not analysis:
            continue
        try:
            s = engine.score(address, analysis, ws)
        except Exception:
            continue
        ranked.append((s.get("confidence", 0), address, analysis, s))
    ranked.sort(reverse=True, key=lambda x: x[0])
    return ranked


def top_buy(md, ws, meta):
    tokens = md.get("tokens", {}) or {}
    ranks = []
    for address, token_data in tokens.items():
        analysis = _analysis(token_data)
        if not analysis:
            continue
        try:
            s = engine.score(address, analysis, ws)
        except Exception:
            continue
        if engine.num(analysis.get("price_in_sda")) <= 0:
            continue
        flow = analysis.get("flow", {}).get("1h", {}) or {}
        trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count"))
        volume_1h = engine.num(flow.get("total_volume"))
        if volume_1h < 250:
            continue
        ranks.append((s.get("confidence", 0), address, s, trades, volume_1h))
    ranks.sort(key=lambda x: (x[0], x[4], x[3]), reverse=True)
    lines = ["🔥 TOP BUY CANDIDATES", ""]
    if not ranks:
        lines.append("⚪ No active candidates (1H volume ≥ 250 SDA)")
        return "\n".join(lines)
    for i, (score, address, s, trades, volume_1h) in enumerate(ranks[:5], 1):
        label = engine.lbl(address, meta)
        icon = "🟢 BUY" if score >= engine.BUY_THRESHOLD and trades >= engine.MIN_TRADES_1H else ("🟡 WATCH" if score >= 55 else "⚪ WEAK")
        lines.append(f"{i}. {icon} {label} — {score:.0f}/100")
        lines.append(f"   1H {s.get('m1h', 0):+.2f}% • flow {s.get('net_1h', 0):+.0f} SDA • vol {volume_1h:.0f} SDA • trades {trades:.0f}")
    return "\n".join(lines)


def market_momentum_report():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    rows = []
    for score, address, analysis, s in _ranked(md, ws):
        flow = analysis.get("flow", {}).get("1h", {}) or {}
        vol = engine.num(flow.get("total_volume"))
        if vol < 250:
            continue
        trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count"))
        rows.append((vol, score, address, s, trades))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines = ["📊 MARKET MOMENTUM", "", "Active filter: 1H volume ≥ 250 SDA", "Trade count is informational only", "────────────────────────"]
    for vol, score, address, s, trades in rows[:10]:
        lines.append(f"{engine.lbl(address, meta)} • {score:.0f}/100")
        lines.append(f"   1H {s.get('m1h', 0):+.2f}% • flow {s.get('net_1h', 0):+.0f} SDA • vol {vol:.0f} SDA • trades {trades:.0f}")
    if len(lines) == 5:
        lines.append("⚪ No active tokens right now")
    return "\n".join(lines)


def market_debug_report():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    tokens = md.get("tokens", {}) or {}
    loaded = len(tokens)
    active = 0
    analyzed = 0
    total_volume = 0.0
    total_trades = 0
    for address, token_data in tokens.items():
        analysis = _analysis(token_data)
        if analysis:
            analyzed += 1
        flow = analysis.get("flow", {}).get("1h", {}) if isinstance(analysis, dict) else {}
        vol = engine.num((flow or {}).get("total_volume"))
        trades = engine.num((flow or {}).get("buy_count")) + engine.num((flow or {}).get("sell_count"))
        total_volume += vol
        total_trades += trades
        if vol >= 250:
            active += 1
    lines = [
        "🐞 MARKET DEBUG",
        "",
        f"Loaded tokens: {loaded}",
        f"Analyzed tokens: {analyzed}",
        f"Active tokens: {active}",
        f"1H volume total: {total_volume:.0f} SDA",
        f"1H trades total: {total_trades:.0f}",
        f"Whale data entries: {len(ws) if isinstance(ws, dict) else 0}",
        "",
        "Active rule: 1H volume ≥ 250 SDA",
        "Trades do not control activity filtering",
    ]
    return "\n".join(lines)


def main_dashboard():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    wallet = load("wallet_data.json", {})
    portfolio = load("portfolio_data.json", {})
    wallet_text = scanner.wallet_message_v16(wallet)
    portfolio_text = scanner.portfolio_message_v16(portfolio, [])
    return (
        "📈 SDA MARKET SCANNER\n\n" +
        top_buy(md, ws, meta) + "\n\n" +
        wallet_text + "\n\n" +
        portfolio_text +
        "\n\n────────────────────────\n📂 DETAIL MENU"
    )


def technical_report():
    md = load("market_data.json", {"tokens": {}})
    meta = load("token_metadata.json", {})
    ws = load("whale_data.json", {})
    lines = ["📐 TECHNICAL ANALYSIS", "", "Analysis only • does not change BUY/SELL logic", "────────────────────────"]
    for score, address, analysis, _ in _ranked(md, ws)[:5]:
        symbol = engine.lbl(address, meta)
        tech = analysis.get("technical") or {}
        if not tech:
            lines += [f"🪙 {symbol}", "   Technical data: N/A", ""]
            continue
        lines.append(f"🪙 {symbol} • scanner {score:.0f}/100")
        for tf in ("1h", "4h", "1d", "1w"):
            x = tech.get(tf, {}) or {}
            trend = x.get("trend", "N/A")
            rsi = x.get("rsi14")
            rsi_txt = f"RSI {rsi:.1f}" if isinstance(rsi, (int, float)) else "RSI N/A"
            lines.append(f"   {tf.upper():<2} {trend:<8} • {rsi_txt}")
        d = tech.get("1d", {}) or {}
        macd = d.get("macd", {}) if isinstance(d.get("macd"), dict) else {}
        hist = macd.get("histogram")
        macd_txt = f"{hist:+.6f}" if isinstance(hist, (int, float)) else "N/A"
        lines.append(f"   MACD 1D histogram: {macd_txt}")
        lines.append(f"   Support: {d.get('support', 'N/A')} • Resistance: {d.get('resistance', 'N/A')}")
        lines.append("")
    lines += ["────────────────────────", "⚪ N/A = insufficient historical coverage"]
    return "\n".join(lines)


def paper_report():
    p = load("positions.json", {"positions": {}, "closed_trades": []})
    positions = p.get("positions", {}) or {}
    closed = p.get("closed_trades", []) or []
    market = load("market_data.json", {"tokens": {}})
    tokens = market.get("tokens", {}) or {}
    realized = sum(engine.num(x.get("closed_profit_sda")) for x in closed)
    open_pnl = 0.0
    invested = 0.0
    for x in positions.values():
        frac = engine.num(x.get("remaining_fraction", 1))
        inv = engine.num(x.get("investment_sda"))
        invested += inv * frac
        entry = engine.num(x.get("entry_price"))
        address = str(x.get("address", "")).lower()
        cur = engine.num(_analysis(tokens.get(address, {}) or {}).get("price_in_sda"))
        if entry > 0 and cur > 0:
            open_pnl += (cur / entry - 1) * inv * frac
    total = realized + open_pnl
    roi = total / invested * 100 if invested else 0.0
    return "\n".join([
        "🤖 SDA PAPER TRADING V18", "", f"Open positions: {len(positions)}", f"Closed trades: {len(closed)}",
        "────────────────────────", f"Realized P/L: {realized:+.2f} SDA", f"Open P/L: {open_pnl:+.2f} SDA",
        f"Cumulative P/L: {total:+.2f} SDA", f"ROI: {roi:+.2f}%", f"Invested: {invested:.2f} SDA", "",
        "Investment per BUY: 50–100 SDA", "Fee: 1.0% • Slippage: 0.1%", "Auto scan: every 5 minutes"
    ])


def handle_update(update, state=None):
    state = state if state is not None else {"offset": 0}
    state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
    cb = update.get("callback_query") or {}
    data = cb.get("data")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    configured_chat = os.environ.get("CHAT_ID")
    if configured_chat and str(chat_id) != str(configured_chat):
        answer_callback(cb.get("id"), "Unauthorized")
        return state
    answer_callback(cb.get("id"))
    reports = {
        "TECH": technical_report,
        "PAPER": paper_report,
        "MOMENTUM": market_momentum_report,
        "DEBUG": market_debug_report,
        "MAIN": main_dashboard,
    }
    if data in reports:
        text = reports[data]()
        edit(chat_id, message_id, text, back_keyboard() if data != "MAIN" else menu_keyboard())
    return state


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
        if not items:
            continue
        for update in items:
            handle_update(update, state)
        save(STATE_FILE, state)
    save(STATE_FILE, state)


def run():
    if "--listen" in sys.argv or os.environ.get("TELEGRAM_MODE") == "listen":
        listen_loop()
        return
    send(main_dashboard(), menu_keyboard())


if __name__ == "__main__":
    run()
