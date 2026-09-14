"""Expose the isolated V25 PUMP-HUNTER in existing Telegram diagnostics.

Does not change MAX-WIN scoring/execution. It only augments the existing
TOP BUY CANDIDATES, MARKET DEBUG and POSITION ACTION reports.
"""
import json


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _state(dashboard):
    try:
        x = dashboard.load("v25_learner_state.json", {})
        return x.get("paper_hunter", {}) if isinstance(x, dict) else {}
    except Exception:
        return {}


def _lane(c):
    """Explain which V25 opportunity lane a candidate is closest to.

    This is diagnostic only; the actual BUY decision remains hunter._gate().
    """
    score = _num(c.get("pump_score"))
    volume = _num(c.get("volume"))
    trades = _num(c.get("trades"))
    m1h = _num(c.get("m1h"))
    m15 = _num(c.get("m15"))
    accel = _num(c.get("accel"))
    bear = _num(c.get("bear"))
    mae = _num(c.get("expected_mae"))
    mfe = _num(c.get("expected_mfe"))
    ev = _num(c.get("expected_ev"))
    p10 = _num(c.get("p10"))
    flow = _num(c.get("flow"))

    if (volume >= 250 and m1h >= 8 and m15 >= 1 and accel >= 5 and bear < 2
            and mae >= -3 and mfe >= 3 and ev >= -0.35 and score >= 40 and p10 >= 0.05):
        return "EARLY-PUMP"
    if (volume >= 250 and trades >= 4 and score >= 40 and bear < 2
            and mae >= -4 and mfe >= 3 and ev >= 0 and p10 >= 0.08
            and (m1h >= 0 or flow > 0 or accel >= 2)):
        return "QUALITY-PUMP"
    if score >= 55:
        return "STANDARD"
    return "WATCH"


def _why_not_buy(c, reasons, allowed):
    if allowed:
        return "BUY GATES PASS"
    if reasons:
        # Keep the full first failure rather than hiding the reason behind a
        # generic WATCH label. This makes strong pumps such as sUSDT auditable.
        return "; ".join(str(x) for x in reasons[:3])
    return "V25 gate blocked"


def _pump_candidates(dashboard, limit=5):
    """Score current market tokens with the same PUMP-HUNTER logic, read-only."""
    try:
        import v25_paper_hunter as hunter
        import main as scanner
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        core = scanner._paper.load(scanner._paper.POSITIONS_FILE, {"positions": {}})
        corepos = core.get("positions", {}) if isinstance(core, dict) else {}
        state = _state(dashboard)
        pump_pos = state.get("positions", {}) if isinstance(state, dict) else {}
        learned = hunter._learned_ready(state) if isinstance(state, dict) else False
        rows = []
        for address, td in (md.get("tokens", {}) or {}).items():
            a = dashboard._analysis(td)
            p = _num(a.get("price_in_sda"))
            key = str(address).lower()
            if not a or p <= 0 or key in pump_pos or key in corepos:
                continue
            c = hunter._features(scanner, key, a, ws)
            allowed, reasons = hunter._gate(c, learned)
            if c["pump_score"] < 40 and not allowed:
                continue
            rank = c["pump_score"] + min(12.0, max(0.0, c["expected_mfe"])) * 0.5 + min(8.0, max(0.0, c["p10"]) * 10.0) + min(5.0, max(0.0, c["accel"])) * 0.25
            rows.append({"rank": rank, "address": key, "label": scanner.lbl(key, meta), "c": c, "allowed": allowed, "reasons": reasons, "lane": _lane(c)})
        rows.sort(key=lambda x: (x["rank"], x["c"]["pump_score"]), reverse=True)
        return rows[:limit], learned
    except Exception:
        return [], False


def _pump_position_rows(dashboard):
    try:
        import v25_paper_hunter as hunter
        import main as scanner
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        state = _state(dashboard)
        rows = []
        for address, pos in (state.get("positions", {}) or {}).items():
            td = (md.get("tokens", {}) or {}).get(str(address).lower(), {})
            an = dashboard._analysis(td)
            price = _num(an.get("price_in_sda"))
            entry = _num(pos.get("entry_price"))
            roi = (price / entry - 1.0) * 100.0 if price > 0 and entry > 0 else 0.0
            c = hunter._features(scanner, str(address).lower(), an, ws) if an else {"pump_score":0,"m1h":0,"m15":0,"flow":0,"accel":0}
            peak = _num(pos.get("peak_price"), entry)
            peak_roi = (peak / entry - 1.0) * 100.0 if entry > 0 else 0.0
            trailing = peak * (1.0 - hunter._trail({"entry_price": entry, "peak_price": peak}, c)) if peak > 0 else 0.0
            if price > 0 and peak_roi >= 12 and price <= trailing:
                action = "PUMP TRAILING EXIT"
                icon = "🔴"
            elif roi >= 10 and c.get("m1h", 0) < -5 and c.get("m15", 0) < -3 and c.get("flow", 0) < 0:
                action = "PUMP REVERSAL EXIT"
                icon = "🔴"
            elif roi > 0 and (c.get("m1h", 0) >= 0 or c.get("flow", 0) > 0):
                action = "PUMP HOLD / TRAIL"
                icon = "🟢"
            elif roi < -7:
                action = "PUMP RISK"
                icon = "🟠"
            else:
                action = "PUMP HOLD / WATCH"
                icon = "🟡"
            rows.append({"symbol": pos.get("symbol") or address, "roi": roi, "peak_roi": peak_roi, "score": c.get("pump_score", 0), "action": action, "icon": icon, "m1h": c.get("m1h", 0), "flow": c.get("flow", 0), "entry": entry, "price": price})
        return rows
    except Exception:
        return []


def _pump_section_top_buy(dashboard, text):
    rows, learned = _pump_candidates(dashboard, 5)
    lines = [text, "", "🚀 PUMP-HUNTER • TOP PUMP CANDIDATES", "────────────────────────", f"Learning: {'READY' if learned else 'WARMING UP'}"]
    if not rows:
        lines.append("⚪ No qualifying pump candidates")
    else:
        for i, r in enumerate(rows, 1):
            c = r["c"]
            status = "🟢 PUMP BUY" if r["allowed"] else "🟡 WATCH"
            pred = f"P10 {c['p10']:.0%} • MFE {c['expected_mfe']:+.1f}%"
            gate = _why_not_buy(c, r["reasons"], r["allowed"])
            lines += [f"{i}. {status} {r['label']} • {r['lane']} • pump {c['pump_score']:.0f}/100", f"   M15/M1H/M4H {c['m15']:+.1f}/{c['m1h']:+.1f}/{c['m4h']:+.1f}% • flow {c['flow']:+.0f} • accel {c['accel']:+.1f}", f"   {pred} • trades {c['trades']:.0f} • vol {c['volume']:.0f}", f"   {'WHY NOT BUY: ' if not r['allowed'] else ''}{gate}"]
    return "\n".join(lines)


def _pump_section_debug(dashboard, text):
    rows, learned = _pump_candidates(dashboard, 5)
    state = _state(dashboard); stats = state.get("stats", {}) if isinstance(state, dict) else {}
    lines = [text, "", "🚀 PUMP-HUNTER • ACTIVE DIAGNOSTIC", "────────────────────────", f"Learning: {'READY' if learned else 'WARMING UP'} • closed {stats.get('closed', 0)} • wins {stats.get('wins', 0)} • realized {stats.get('realized_pnl_sda', 0):+.2f} SDA", f"Open pump positions: {len(state.get('positions', {}) or {})} • WATCH: {len(state.get('watch', {}) or {})}"]
    if rows:
        lines.append("")
        lines.append("Top pump signals:")
        for i, r in enumerate(rows, 1):
            c = r["c"]
            status = "BUY" if r["allowed"] else "WATCH"
            why = _why_not_buy(c, r["reasons"], r["allowed"])
            lines.append(f"{i}. {status} {r['label']} • {r['lane']} • pump {c['pump_score']:.0f} • M1H {c['m1h']:+.1f}% • flow {c['flow']:+.0f} • P10 {c['p10']:.0%} • MFE {c['expected_mfe']:+.1f}%")
            if not r["allowed"]:
                lines.append(f"   WHY NOT BUY: {why}")
    return "\n".join(lines)


def _pump_section_actions(dashboard, text):
    rows = _pump_position_rows(dashboard)
    lines = [text, "", "🚀 PUMP-HUNTER • POSITION ACTION", "────────────────────────"]
    if not rows:
        lines.append("⚪ No open pump positions")
    else:
        for r in sorted(rows, key=lambda x: x["roi"], reverse=True):
            lines += [f"{r['icon']} {r['symbol']}: {r['action']} • P/L {r['roi']:+.2f}% • pump {r['score']:.0f}/100", f"   peak {r['peak_roi']:+.1f}% • M1H {r['m1h']:+.1f}% • flow {r['flow']:+.0f}"]
    return "\n".join(lines)


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_pump_dashboard_patched", False):
        return dashboard
    original_top_buy = dashboard.top_buy
    original_debug = dashboard.market_debug_report
    original_real = dashboard.real_trading_report

    def top_buy(*args, **kwargs):
        return _pump_section_top_buy(dashboard, original_top_buy(*args, **kwargs))

    def market_debug_report(*args, **kwargs):
        return _pump_section_debug(dashboard, original_debug(*args, **kwargs))

    def real_trading_report(*args, **kwargs):
        return _pump_section_actions(dashboard, original_real(*args, **kwargs))

    dashboard.top_buy = top_buy
    dashboard.market_debug_report = market_debug_report
    dashboard.real_trading_report = real_trading_report
    dashboard._sda_pump_dashboard_patched = True
    return dashboard
