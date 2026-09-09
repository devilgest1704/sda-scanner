#!/usr/bin/env python3
import json
import os
import requests

import engine
import main as scanner

STATE_FILE = "telegram_menu_state.json"


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, type(default)) else default
    except Exception:
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


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


def answer_callback(callback_id):
    if callback_id:
        api("answerCallbackQuery", {"callback_query_id": callback_id})


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📐 Technical Analysis", "callback_data": "TECH"}],
        [{"text": "🤖 Paper Trading", "callback_data": "PAPER"}],
    ]}


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}


def top_buy(md, ws, meta):
    tokens = md.get("tokens", {}) or {}
    ranks = []
    for address, analysis in tokens.items():
        if not isinstance(analysis, dict):
            continue
        try:
            s = engine.score(address, analysis, ws)
        except Exception:
            continue
        if engine.num(analysis.get("price_in_sda")) <= 0:
            continue
        flow = analysis.get("flow", {}).get("1h", {}) or {}
        trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count"))
        ranks.append((s.get("confidence", 0), address, s, trades))
    ranks.sort(key=lambda x: (x[0], x[3]), reverse=True)
    lines = ["🔥 TOP BUY CANDIDATES", ""]
    for i, (score, address, s, trades) in enumerate(ranks[:5], 1):
        label = engine.lbl(address, meta)
        icon = "🟢 BUY" if score >= engine.BUY_THRESHOLD and trades >= engine.MIN_TRADES_1H else ("🟡 WATCH" if score >= 55 else "⚪ WEAK")
        lines.append(f"{i}. {icon} {label} — {score:.0f}/100")
        lines.append(f"   1H {s.get('m1h', 0):+.2f}% • flow {s.get('net_1h', 0):+.0f} SDA • trades {trades:.0f}")
    return "\n".join(lines)


def main_dashboard():
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    wallet = load("wallet_data.json", {})
    portfolio = load("portfolio_data.json", {})
    wallet_text = scanner.wallet_message_v16(wallet)
    portfolio_text = scanner.portfolio_message_v16(portfolio, [])
    return ("📈 SDA MARKET SCANNER\n\n" + top_buy(md, ws, meta) + "\n\n" +
            wallet_text + "\n\n" + portfolio_text +
            "\n\n────────────────────────\n📂 DETAIL MENU\nTechnical Analysis and Paper Trading are available below.")


def technical_report():
    md = load("market_data.json", {"tokens": {}})
    meta = load("token_metadata.json", {})
    ws = load("whale_data.json", {})
    tokens = md.get("tokens", {}) or {}
    ranked = []
    for address, analysis in tokens.items():
        try:
            s = engine.score(address, analysis, ws)
            ranked.append((s.get("confidence", 0), address, analysis))
        except Exception:
            pass
    ranked.sort(reverse=True, key=lambda x: x[0])

    lines = ["📐 TECHNICAL ANALYSIS", "", "Analysis only • does not change BUY/SELL logic", "────────────────────────"]
    for score, address, analysis in ranked[:5]:
        symbol = engine.lbl(address, meta)
        tech = (analysis.get("technical") or {}) if isinstance(analysis, dict) else {}
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
        support = d.get("support", "N/A")
        resistance = d.get("resistance", "N/A")
        lines.append(f"   MACD 1D histogram: {macd_txt}")
        lines.append(f"   Support: {support} • Resistance: {resistance}")
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
        cur = engine.num((tokens.get(address, {}) or {}).get("analysis", {}).get("price_in_sda"))
        if entry > 0 and cur > 0:
            open_pnl += (cur / entry - 1) * inv * frac
    total = realized + open_pnl
    roi = total / invested * 100 if invested else 0.0
    lines = ["🤖 SDA PAPER TRADING V18", "", f"Open positions: {len(positions)}", f"Closed trades: {len(closed)}", "────────────────────────", f"Realized P/L: {realized:+.2f} SDA", f"Open P/L: {open_pnl:+.2f} SDA", f"Cumulative P/L: {total:+.2f} SDA", f"ROI: {roi:+.2f}%", f"Invested: {invested:.2f} SDA", "", "Investment per BUY: 50–100 SDA", "Fee: 1.0% • Slippage: 0.1%", "Auto scan: every 5 minutes"]
    return "\n".join(lines)


def handle_updates():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return
    state = load(STATE_FILE, {"offset": 0})
    offset = int(state.get("offset", 0))
    result = api("getUpdates", {"offset": offset, "timeout": 1, "allowed_updates": ["callback_query"]})
    items = (result or {}).get("result", []) if isinstance(result, dict) else []
    for update in items:
        state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
        cb = update.get("callback_query") or {}
        data = cb.get("data")
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        answer_callback(cb.get("id"))
        if data == "TECH":
            edit(chat_id, message_id, technical_report(), back_keyboard())
        elif data == "PAPER":
            edit(chat_id, message_id, paper_report(), back_keyboard())
        elif data == "MAIN":
            edit(chat_id, message_id, main_dashboard(), menu_keyboard())
    save(STATE_FILE, state)


def run():
    handle_updates()
    send(main_dashboard(), menu_keyboard())


if __name__ == "__main__":
    run()
