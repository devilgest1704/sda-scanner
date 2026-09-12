#!/usr/bin/env python3
import json
import os
import sys
import time
import requests

import engine
import main as scanner
from strategy_v21 import patch_engine, technical_confirmation

# Use the same V21 scoring overlay as the paper engine.
patch_engine(engine)

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


def paper_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📊 Statistics", "callback_data": "PAPER_STATS"}],
        [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
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


def _buy_gate_rows(md, ws, meta, limit=5):
    """Return current TOP BUY candidates with their exact BUY gates."""
    rows = []
    tokens = md.get("tokens", {}) or {}
    for address, token_data in tokens.items():
        analysis = _analysis(token_data)
        if not analysis or engine.num(analysis.get("price_in_sda")) <= 0:
            continue
        flow = analysis.get("flow", {}).get("1h", {}) or {}
        trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count"))
        volume_1h = engine.num(flow.get("total_volume"))
        if volume_1h < 250:
            continue
        try:
            s = engine.score(address, analysis, ws)
        except Exception:
            continue
        score = engine.num(s.get("confidence"))
        reasons = []
        if score < engine.BUY_THRESHOLD:
            reasons.append(f"score {score:.0f}<{engine.BUY_THRESHOLD}")
        if trades < engine.MIN_TRADES_1H:
            reasons.append(f"trades {trades:.0f}<{engine.MIN_TRADES_1H}")
        if not reasons:
            reasons.append("ALL BUY GATES PASS")
        rows.append({"address": address, "label": engine.lbl(address, meta), "analysis": analysis, "score_data": s, "score": score, "trades": trades, "volume_1h": volume_1h, "m1h": engine.num(s.get("m1h")), "net_1h": engine.num(s.get("net_1h")), "whale_net": engine.num(s.get("whale_net")), "reasons": reasons})
    rows.sort(key=lambda x: (x["score"], x["volume_1h"], x["trades"]), reverse=True)
    return rows[:limit]


def _snapshot_id(md, ws, meta):
    """Stable identifier for the exact market/whale/metadata snapshot."""
    payload = {
        "market_data": md,
        "whale_data": ws,
        "token_metadata": meta,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _snapshot_time(md, ws):
    for data in (md, ws):
        if isinstance(data, dict):
            for key in ("updated_at", "timestamp", "generated_at", "fetched_at"):
                value = data.get(key)
                if value:
                    return str(value)
    return "N/A"


def _load_dashboard_snapshot(limit=5):
    """Load market state once and score each candidate once."""
    md = load("market_data.json", {"tokens": {}})
    ws = load("whale_data.json", {})
    meta = load("token_metadata.json", {})
    rows = _buy_gate_rows(md, ws, meta, limit)
    return {
        "md": md,
        "ws": ws,
        "meta": meta,
        "rows": rows,
        "id": _snapshot_id(md, ws, meta),
        "time": _snapshot_time(md, ws),
    }


def top_buy(md, ws, meta, rows=None, snapshot=None):
    rows = rows if rows is not None else _buy_gate_rows(md, ws, meta, 5)
    lines = ["🔥 TOP BUY CANDIDATES", ""]
    if snapshot:
        lines.append(f"Snapshot: {snapshot['id']} • {snapshot['time']}")
        lines.append("")
    if not rows:
        lines.append("⚪ No active candidates (1H volume ≥ 250 SDA)")
        return "\n".join(lines)
    for i, row in enumerate(rows, 1):
        score = row["score"]
        icon = "🟢 BUY" if score >= engine.BUY_THRESHOLD and row["trades"] >= engine.MIN_TRADES_1H else ("🟡 WATCH" if score >= 55 else "⚪ WEAK")
        lines.append(f"{i}. {icon} {row['label']} — {score:.0f}/100")
        lines.append(f"   1H {row['m1h']:+.2f}% • flow {row['net_1h']:+.0f} SDA • vol {row['volume_1h']:.0f} SDA • trades {row['trades']:.0f}")
    return "\n".join(lines)


def market_momentum_report():
    md = load("market_data.json", {"tokens": {}}); ws = load("whale_data.json", {}); meta = load("token_metadata.json", {})
    rows = []
    for score, address, analysis, s in _ranked(md, ws):
        flow = analysis.get("flow", {}).get("1h", {}) or {}; vol = engine.num(flow.get("total_volume"))
        if vol < 250: continue
        trades = engine.num(flow.get("buy_count")) + engine.num(flow.get("sell_count")); rows.append((vol, score, address, s, trades))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines = ["📊 MARKET MOMENTUM", "", "Active filter: 1H volume ≥ 250 SDA", "Trade count is informational only", "────────────────────────"]
    for vol, score, address, s, trades in rows[:10]:
        lines.append(f"{engine.lbl(address, meta)} • {score:.0f}/100"); lines.append(f"   1H {s.get('m1h', 0):+.2f}% • flow {s.get('net_1h', 0):+.0f} SDA • vol {vol:.0f} SDA • trades {trades:.0f}")
    if len(lines) == 5: lines.append("⚪ No active tokens right now")
    return "\n".join(lines)


def _score_breakdown(addr, analysis, ws):
    """Legacy BASE diagnostic only; FINAL must always come from engine.score()."""
    m = analysis.get("momentum", {}) or {}
    f1 = engine.flow(analysis, "1h")
    f15 = engine.flow(analysis, "15m")
    m15 = engine.num(m.get("15m_pct")); m1 = engine.num(m.get("1h_pct")); m4 = engine.num(m.get("4h_pct"))
    bv = engine.num(f1.get("buy_volume")); sv = engine.num(f1.get("sell_volume")); bc = engine.num(f1.get("buy_count")); sc = engine.num(f1.get("sell_count"))
    tv = bv + sv; tt = bc + sc; strength = bv / max(sv, 1); tratio = bc / max(sc, 1)
    w15 = engine.whale(addr, ws, "15m"); w1 = engine.whale(addr, ws, "1h"); w4 = engine.whale(addr, ws, "4h")
    wn = engine.num(w1.get("net_flow")); wb = engine.num(w1.get("buy_volume")); wsell = engine.num(w1.get("sell_volume")); wbc = engine.num(w1.get("buy_count")); wsc = engine.num(w1.get("sell_count")); wn15 = engine.num(w15.get("net_flow")); wn4 = engine.num(w4.get("net_flow"))
    components = []
    def add(name, value): components.append((name, float(value)))
    add("1H momentum", max(0, min(1, (m1 + 1) / 9)) * 18)
    add("4H momentum", max(0, min(1, (m4 + 3) / 10)) * 8)
    add("B/S volume", max(0, min(1, (strength - .8) / 1.7)) * 18)
    add("B/S trades", max(0, min(1, (tratio - .8) / 1.7)) * 7)
    avail = wb > 0 or wsell > 0 or wbc > 0 or wsc > 0
    if wn > 0: add("Whale 1H net", max(0, min(1, wn / 10000)) * 20)
    elif wn < 0: add("Whale 1H net", max(-10, min(0, wn / 10000)))
    else: add("Whale 1H net", 0)
    add("Whale buy count", 3 if wbc >= 2 else 0)
    add("Whale sell count", -3 if wsc >= 3 and wn < 0 else 0)
    add("Whale 15M net", 4 if wn15 > 0 else (-3 if wn15 < 0 else 0))
    if m1 >= 2 and m15 > .2: add("Momentum combo", 10)
    elif m1 >= 1 and m15 >= 0: add("Momentum combo", 6)
    elif m1 >= 4 and m15 < -.5: add("Momentum combo", -5)
    else: add("Momentum combo", 0)
    n15 = engine.num(f15.get("net_flow")); add("15M net flow", 3 if n15 > 0 else (-2 if n15 < 0 else 0))
    va = analysis.get("volume_acceleration_15m_pct"); va_n = engine.num(va) if va is not None else None
    add("Volume acceleration", 4 if va_n is not None and va_n > 15 else (-2 if va_n is not None and va_n < -20 else 0))
    add("1H total volume", max(0, min(1, tv / 15000)) * 7)
    add("1H trade count", 5 if tt >= 8 else (3 if tt >= 4 else (1 if tt >= 2 else -8)))
    raw_sum = sum(v for _, v in components)
    final = int(round(max(0, min(100, raw_sum))))
    cap_applied = False
    if not avail:
        capped = min(final, 72); cap_applied = capped != final; final = capped
    return {"components": components, "final": final, "raw_sum": raw_sum, "m15": m15, "m1": m1, "m4": m4, "bv": bv, "sv": sv, "bc": bc, "sc": sc, "tv": tv, "tt": tt, "strength": strength, "tratio": tratio, "wn": wn, "wbc": wbc, "wsc": wsc, "wn15": wn15, "wn4": wn4, "n15": n15, "va": va_n, "wb": wb, "wsell": wsell, "avail": avail, "cap_applied": cap_applied}


def market_debug_report(snapshot=None):
    if snapshot is None:
        snapshot = _load_dashboard_snapshot(5)

    md = snapshot["md"]
    ws = snapshot["ws"]
    meta = snapshot["meta"]
    rows = snapshot["rows"]
    tokens = md.get("tokens", {}) or {}

    loaded = len(tokens)
    active = 0
    analyzed = 0
    total_volume = 0.0
    total_trades = 0
    for token_data in tokens.values():
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
        f"Snapshot: {snapshot['id']}",
        f"State time: {snapshot['time']}",
        f"Loaded tokens: {loaded}",
        f"Analyzed tokens: {analyzed}",
        f"Active tokens: {active}",
        f"1H volume total: {total_volume:.0f} SDA",
        f"1H trades total: {total_trades:.0f}",
        f"Whale data entries: {len(ws) if isinstance(ws, dict) else 0}",
        "",
        "Active rule: 1H volume ≥ 250 SDA",
        "Trades do not control activity filtering",
        "",
        "🎯 TOP BUY CANDIDATE DIAGNOSTICS",
        f"BUY threshold: {engine.BUY_THRESHOLD}/100",
        f"Min trades: {engine.MIN_TRADES_1H}",
        "────────────────────────",
    ]

    if not rows:
        lines.append("⚪ No active TOP BUY candidates")
    else:
        for i, row in enumerate(rows, 1):
            bd = _score_breakdown(row["address"], row["analysis"], ws)
            tc = technical_confirmation(row["analysis"])
            tech_available = bool(row["analysis"].get("technical"))
            bull = tc["bull"]
            bear = tc["bear"]
            adj = min(8, bull * 1.5) - min(10, bear * 1.8)
            v21_score = engine.num(row["score"])
            blocked = [x for x in row["reasons"] if x != "ALL BUY GATES PASS"]
            status = "🔴 BLOCKED" if blocked else "🟢 BUY READY"

            lines.append(f"{i}. {status} {row['label']} — {v21_score:.0f}/100")
            lines.append(f"   BASE diagnostic: {bd['final']:.0f}/100 (raw {bd['raw_sum']:+.1f})")
            for name, value in bd["components"]:
                lines.append(f"      {name}: {value:+.2f}")
            lines.append(
                f"   INPUT: 1H {bd['m1']:+.2f}% | 4H {bd['m4']:+.2f}% | "
                f"15M {bd['m15']:+.2f}% | volume {bd['tv']:.0f} | trades {bd['tt']:.0f}"
            )
            lines.append(
                f"   B/S: volume {bd['bv']:.0f}/{bd['sv']:.0f} ({bd['strength']:.2f}x) | "
                f"trades {bd['bc']:.0f}/{bd['sc']:.0f} ({bd['tratio']:.2f}x)"
            )
            lines.append(
                f"   WHALE: 1H {bd['wn']:+.0f} | buys {bd['wbc']:.0f} | "
                f"sells {bd['wsc']:.0f} | 15M {bd['wn15']:+.0f} | 4H {bd['wn4']:+.0f}"
            )
            va_text = "N/A" if bd["va"] is None else f"{bd['va']:+.1f}%"
            lines.append(f"   FLOW: 15M {bd['n15']:+.0f} | volume accel {va_text}")
            lines.append(
                f"   V21 technical diagnostic: bull {bull} | bear {bear} | "
                f"adjustment {adj:+.1f} | technical data {'YES' if tech_available else 'N/A'}"
            )
            if tc["evidence"]:
                lines.append(f"      Evidence: {', '.join(tc['evidence'])}")
            lines.append(f"   FINAL: {v21_score:.0f}/100  ← canonical engine.score()")
            lines.append(f"   Reason: {'; '.join(row['reasons'])}")

    return "\n".join(lines)


def _resolve_market_token(token, pf, tokens, meta):
    """Resolve a portfolio key/symbol to the market-data address."""
    candidates = [token, pf.get("address") if isinstance(pf, dict) else None, pf.get("token_address") if isinstance(pf, dict) else None]
    for c in candidates:
        if not c:
            continue
        c = str(c).strip()
        if c in tokens:
            return c
        if c.lower() in tokens:
            return c.lower()
    wanted = str(pf.get("symbol") if isinstance(pf, dict) and pf.get("symbol") else token).strip().upper()
    for address, td in tokens.items():
        label = engine.lbl(address, meta)
        raw = td.get("analysis", td) if isinstance(td, dict) else {}
        sym = raw.get("symbol") or td.get("symbol") if isinstance(td, dict) else None
        if str(label).split("/", 1)[0].strip().upper() == wanted or str(sym or "").strip().upper() == wanted:
            return str(address)
    return str(token).lower() if str(token).lower() in tokens else ""


def _position_recommendations(md, ws, meta, portfolio):
    state = load("decision_state_v15.json", {}); cur = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}; tokens = md.get("tokens", {}) if isinstance(md, dict) else {}; out = []
    for token, pf in cur.items():
        address = _resolve_market_token(token, pf, tokens, meta)
        an = _analysis(tokens.get(address, {}) or {}) if address else {}
        try:
            s = engine.score(address, an, ws) if address and an else {"confidence": None, "m1h": None, "net_1h": None}
        except Exception:
            s = {"confidence": None, "m1h": None, "net_1h": None}
        score_raw = s.get("confidence"); score = engine.num(score_raw) if score_raw is not None else None; m1_raw = s.get("m1h"); flow_raw = s.get("net_1h"); m1h = engine.num(m1_raw) if m1_raw is not None else None; flow = engine.num(flow_raw) if flow_raw is not None else None
        pnl_raw = pf.get("unrealized_pnl_pct"); pnl = engine.num(pnl_raw) if pnl_raw is not None else None; old = state.get(token, {}) if isinstance(state.get(token), dict) else {}; neg = int(engine.num(old.get("negative_count"))); weak = int(engine.num(old.get("profit_weakening_count"))); loss_weak = int(engine.num(old.get("loss_weakening_count")))
        negative = score is not None and m1h is not None and flow is not None and score < 35 and m1h < 0 and flow < 0; deep = pnl is not None and m1h is not None and flow is not None and pnl <= -15 and m1h < 0 and flow < 0; weakening = pnl is not None and score is not None and m1h is not None and flow is not None and pnl > 0 and score < 40 and (m1h < 0 or flow < 0); loss_weakening = pnl is not None and score is not None and m1h is not None and flow is not None and pnl <= -8 and (score < 20 or (pnl <= -12 and score < 35)) and (m1h < 0 or flow < 0)
        neg = min(5, neg + 1) if (negative or deep) else 0; weak = min(5, weak + 1) if weakening else 0; loss_weak = min(5, loss_weak + 1) if loss_weakening else 0
        if not address or not an or score is None: action = "HOLD / MARKET DATA N/A"
        elif pf.get("cost_sda") is None or pnl_raw is None: action = "HOLD / NO COST BASIS"
        elif weak >= 2: action = "PARTIAL SELL"
        elif neg >= 3: action = "SELL / EXIT"
        elif loss_weak >= 3: action = "SELL / EXIT"
        elif score >= 70 and m1h > 0 and flow > 0: action = "HOLD / TRAIL"
        else: action = "HOLD / WATCH"
        out.append({"token": token, "address": address, "symbol": pf.get("symbol") or engine.lbl(address, meta), "action": action, "pnl_sda": pf.get("unrealized_pnl_sda"), "score": score})
    order = {"SELL / EXIT": 0, "PARTIAL SELL": 1, "HOLD / TRAIL": 2, "HOLD / WATCH": 3, "HOLD / NO COST BASIS": 4, "HOLD / MARKET DATA N/A": 5}; return sorted(out, key=lambda x: (order.get(x["action"], 9), -(x["score"] if x["score"] is not None else -1)))


def _merge_wallet_portfolio(wallet, portfolio, md, ws, meta):
    holdings = wallet.get("holdings", []) if isinstance(wallet, dict) else []
    if not isinstance(holdings, list): holdings = []
    portfolio_current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    if not isinstance(portfolio_current, dict): portfolio_current = {}
    portfolio_by_symbol = {str(k).upper(): v for k, v in portfolio_current.items() if isinstance(v, dict)}; entries = {}
    for data in holdings:
        if not isinstance(data, dict): continue
        token = str(data.get("symbol") or "").strip()
        if not token: continue
        entries[token.upper()] = {"token": token, "symbol": token, "amount": data.get("amount", 0), "value_sda": data.get("value_sda"), "price_sda": data.get("price_sda")}
    for token, pf in portfolio_by_symbol.items():
        if token not in entries: entries[token] = {"token": token, "symbol": pf.get("symbol") or engine.lbl(token, meta), "amount": pf.get("amount", pf.get("quantity", 0)), "value_sda": None, "price_sda": None}
        entries[token]["pnl_sda"] = pf.get("unrealized_pnl_sda"); entries[token]["pnl_pct"] = pf.get("unrealized_pnl_pct")
    native_sda = engine.num(wallet.get("native_sda", 0)) if isinstance(wallet, dict) else 0.0; lines = ["👛 REAL WALLET & PORTFOLIO", "", f"💰 SDA: {native_sda:.4f}", f"🪙 Token positions: {len(entries)}", "────────────────────────"]
    for key in sorted(entries):
        row = entries[key]; token = row["token"]; symbol = row.get("symbol") or token; amount = engine.num(row.get("amount", 0)); value = row.get("value_sda"); pnl = row.get("pnl_sda"); pnl_pct = row.get("pnl_pct"); price = row.get("price_sda"); amount_txt = f"{amount:,.4f}" if not float(amount).is_integer() else f"{amount:,.0f}"; value_txt = f"{engine.num(value):.2f} SDA" if value is not None and engine.num(value) >= 0 else "UNKNOWN SDA"; price_txt = f"{engine.num(price):.6f} SDA" if price is not None and engine.num(price) > 0 else "UNKNOWN"
        lines.append(f"🪙 {token} — {symbol}"); lines.append(f"   {amount_txt} {token} • {value_txt} • Price {price_txt}")
        if pnl is not None:
            pnl_n = engine.num(pnl); pct_n = engine.num(pnl_pct); icon = "🟢" if pnl_n > 0 else ("🔴" if pnl_n < 0 else "⚪"); lines.append(f"   {icon} P/L {pnl_n:+.2f} SDA ({pct_n:+.2f}%)")
        lines.append("")
    lines.append("────────────────────────"); lines.append(f"📊 Known token value: {engine.num(wallet.get('total_token_value_sda', 0)):.2f} SDA"); lines.append(f"💼 TOTAL WALLET VALUE: {engine.num(wallet.get('total_value_sda', native_sda)):.2f} SDA"); return "\n".join(lines)


def main_dashboard():
    snapshot = _load_dashboard_snapshot(5)
    md = snapshot["md"]; ws = snapshot["ws"]; meta = snapshot["meta"]
    wallet = load("wallet_data.json", {}); portfolio = load("portfolio_data.json", {})
    recommendations = _position_recommendations(md, ws, meta, portfolio); position_lines = []
    for x in recommendations:
        action = x["action"]
        icon = {
            "EMERGENCY SELL": "🚨",
            "SELL / EXIT": "🔴",
            "PARTIAL SELL": "🟠",
            "HOLD / TRAIL": "🟢",
            "HOLD / WATCH": "🟡",
            "HOLD / NO COST BASIS": "🟡",
            "HOLD / MARKET DATA N/A": "🟡",
        }.get(action, "🟡")
        pnl = "UNKNOWN" if x["pnl_sda"] is None else f"{engine.num(x['pnl_sda']):+.2f} SDA"; score_txt = "N/A" if x["score"] is None else f"{x['score']:.0f}/100"; position_lines.append(f"{icon} {x['symbol']}: {x['action']}  •  P/L {pnl}  •  score {score_txt}")
    position_action = "\n".join(position_lines) if position_lines else "⚪ No portfolio positions"; return "📈 SDA MARKET SCANNER\n\n" + top_buy(md, ws, meta, rows=snapshot["rows"], snapshot=snapshot) + "\n\n" + "\n\n🧭 POSITION ACTION\n" + position_action + "\n\n────────────────────────\n📂 DETAIL MENU"


def technical_report():
    md = load("market_data.json", {"tokens": {}}); meta = load("token_metadata.json", {}); ws = load("whale_data.json", {}); lines = ["📐 TECHNICAL ANALYSIS", "", "TOP BUY CANDIDATES • analysis only • does not change BUY/SELL logic", "────────────────────────"]; rows = _buy_gate_rows(md, ws, meta, 5)
    if not rows: lines.append("⚪ No active TOP BUY candidates")
    for i, row in enumerate(rows, 1):
        tech = row["analysis"].get("technical") or {}; lines.append(f"{i}. 🪙 {row['label']} • scanner {row['score']:.0f}/100"); lines.append(f"   1H {row['m1h']:+.2f}% • flow {row['net_1h']:+.0f} SDA • vol {row['volume_1h']:.0f} SDA • trades {row['trades']:.0f}")
        if not tech: lines.append("   Technical data: N/A"); lines.append(""); continue
        for tf in ("1h", "4h", "1d", "1w"):
            x = tech.get(tf, {}) or {}; trend = x.get("trend", "N/A"); rsi = x.get("rsi_14"); rsi_txt = f"RSI {rsi:.1f}" if isinstance(rsi, (int, float)) else "RSI N/A"; lines.append(f"   {tf.upper():<2} {trend:<18} • {rsi_txt}")
        d = tech.get("1d", {}) or {}; macd = d.get("macd", {}) if isinstance(d.get("macd"), dict) else {}; hist = macd.get("histogram"); macd_txt = f"{hist:+.6f}" if isinstance(hist, (int, float)) else "N/A"; support = d.get("support", "N/A"); resistance = d.get("resistance", "N/A"); ds = d.get("distance_to_support_pct"); dr = d.get("distance_to_resistance_pct"); ds_txt = f"{ds:+.2f}%" if isinstance(ds, (int, float)) else "N/A"; dr_txt = f"{dr:+.2f}%" if isinstance(dr, (int, float)) else "N/A"; lines.append(f"   MACD 1D histogram: {macd_txt}"); lines.append(f"   Support: {support} ({ds_txt})"); lines.append(f"   Resistance: {resistance} ({dr_txt})"); lines.append("")
    lines += ["────────────────────────", "⚪ N/A = insufficient historical coverage"]; return "\n".join(lines)


def _pnl_value(value, unit):
    value = engine.num(value); icon = "🟢" if value > 0 else ("🔴" if value < 0 else "⚪"); return f"{icon} {value:+.2f} {unit}"


def paper_report():
    p = load("positions.json", {"positions": {}, "closed_trades": []}); positions = p.get("positions", {}) or {}; closed = p.get("closed_trades", []) or []; market = load("market_data.json", {"tokens": {}}); tokens = market.get("tokens", {}) or {}; realized = sum(engine.num(x.get("closed_profit_sda")) for x in closed); open_pnl = 0.0; invested = 0.0
    for x in positions.values():
        frac = engine.num(x.get("remaining_fraction", 1)); inv = engine.num(x.get("investment_sda")); invested += inv * frac; entry = engine.num(x.get("entry_price")); address = str(x.get("address", "")).lower(); cur = engine.num(_analysis(tokens.get(address, {}) or {}).get("price_in_sda"));
        if entry > 0 and cur > 0: open_pnl += (cur / entry - 1) * inv * frac
    total = realized + open_pnl; roi = total / invested * 100 if invested else 0.0; return "\n".join(["🤖 SDA PAPER TRADING V18", "", f"Open positions: {len(positions)}", f"Closed trades: {len(closed)}", "────────────────────────", f"Realized P/L: {_pnl_value(realized, 'SDA')}", f"Open P/L: {_pnl_value(open_pnl, 'SDA')}", f"Cumulative P/L: {_pnl_value(total, 'SDA')}", f"ROI: {_pnl_value(roi, '%')}", f"Invested: {invested:.2f} SDA", "", "Investment per BUY: 50–100 SDA", "Fee: 1.0% • Slippage: 0.1%", "Auto scan: every 5 minutes"])


def paper_statistics_report():
    p = load("positions.json", {"positions": {}, "closed_trades": []}); positions = p.get("positions", {}) or {}; closed = p.get("closed_trades", []) or {}; market = load("market_data.json", {"tokens": {}}); tokens = market.get("tokens", {}) or {}; meta = load("token_metadata.json", {})
    if not isinstance(closed, list): closed = []
    realized = sum(engine.num(x.get("closed_profit_sda")) for x in closed); open_pnl = 0.0; invested = 0.0; open_rows = []
    for x in positions.values():
        frac = engine.num(x.get("remaining_fraction", 1)); inv = engine.num(x.get("investment_sda")); active_inv = inv * frac; invested += active_inv; entry = engine.num(x.get("entry_price")); address = str(x.get("address", "")).lower(); an = _analysis(tokens.get(address, {}) or {}); cur = engine.num(an.get("price_in_sda")); pnl = (cur / entry - 1) * active_inv if entry > 0 and cur > 0 else 0.0; open_pnl += pnl; label = x.get("label") or engine.lbl(address, meta); open_rows.append((str(x.get("opened_at", "")), label, entry, cur, active_inv, pnl, engine.num(x.get("entry_confidence"))))
    total = realized + open_pnl; roi = total / invested * 100 if invested else 0.0
    lines = ["📊 PAPER TRADING • STATISTICS", "", f"🟢 Open positions: {len(positions)}", f"📁 Closed trades: {len(closed)}", "────────────────────────", f"Realized P/L: {_pnl_value(realized, 'SDA')}", f"Open P/L: {_pnl_value(open_pnl, 'SDA')}", f"Cumulative P/L: {_pnl_value(total, 'SDA')}", f"ROI on open capital: {_pnl_value(roi, '%')}", f"Open capital: {invested:.2f} SDA"]
    lines += ["", "📌 CURRENT POSITIONS", "────────────────────────"]
    if not open_rows: lines.append("⚪ No open paper positions")
    else:
        for _, label, entry, cur, inv, pnl, confidence in sorted(open_rows, reverse=True):
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪"); change = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0.0; lines.append(f"{icon} {label}"); lines.append(f"   Entry {entry:.6f} → {cur:.6f} SDA"); lines.append(f"   P/L {pnl:+.2f} SDA ({change:+.2f}%) • {inv:.0f} SDA • score {confidence:.0f}")
    lines += ["", "📜 HISTORY", "────────────────────────"]
    if not closed: lines.append("⚪ No closed trades yet")
    else:
        for z in sorted(closed, key=lambda x: str(x.get("closed_at", "")), reverse=True)[:15]:
            label = z.get("label") or z.get("address", "UNKNOWN"); profit = engine.num(z.get("closed_profit_sda")); roi_z = engine.num(z.get("closed_roi_pct")); reason = z.get("close_reason", "EXIT"); icon = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪"); lines.append(f"{icon} {label} • {reason}"); lines.append(f"   {profit:+.2f} SDA ({roi_z:+.2f}%) • {str(z.get('closed_at',''))[:16].replace('T',' ')}")
    lines += ["", "────────────────────────", "Fee 1.0% • Slippage 0.1% • Paper only"]; return "\n".join(lines)


def handle_update(update, state=None):
    state = state if state is not None else {"offset": 0}; state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1); cb = update.get("callback_query") or {}; data = cb.get("data"); msg = cb.get("message") or {}; chat_id = (msg.get("chat") or {}).get("id"); message_id = msg.get("message_id"); configured_chat = os.environ.get("CHAT_ID")
    if configured_chat and str(chat_id) != str(configured_chat): answer_callback(cb.get("id"), "Unauthorized"); return state
    answer_callback(cb.get("id"))
    reports = {"TECH": technical_report, "PAPER_STATS": paper_statistics_report, "MOMENTUM": market_momentum_report, "DEBUG": market_debug_report, "MAIN": main_dashboard}
    if data == "PAPER": edit(chat_id, message_id, "🤖 PAPER TRADING\n\nVyber zobrazení:", paper_menu_keyboard())
    elif data in reports: edit(chat_id, message_id, reports[data](), back_keyboard() if data != "MAIN" else menu_keyboard())
    return state


def save(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)


def listen_loop(duration_seconds=240):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token: print("TELEGRAM_TOKEN is not configured"); return
    state = load(STATE_FILE, {"offset": 0}); deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        result = api("getUpdates", {"offset": int(state.get("offset", 0)), "timeout": 20, "allowed_updates": ["callback_query"]}); items = (result or {}).get("result", []) if isinstance(result, dict) else []
        if not items: continue
        for update in items: handle_update(update, state)
        save(STATE_FILE, state)
    save(STATE_FILE, state)


def run():
    if "--listen" in sys.argv or os.environ.get("TELEGRAM_MODE") == "listen": listen_loop(); return
    send(main_dashboard(), menu_keyboard())


if __name__ == "__main__": run()
