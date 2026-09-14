"""Expose the isolated V25 PUMP-HUNTER in existing Telegram diagnostics.

Does not change MAX-WIN scoring/execution. Dashboard diagnostics use the
canonical V25-aware candidate rows already installed by dashboard_v23_patch,
so TOP BUY CANDIDATES and MARKET DEBUG cannot disagree about which pumps are
being shown. V25 BUY eligibility remains read-only and gate-driven.
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


def _canonical_rows(dashboard, limit=5):
    """Return the exact V25-aware rows used by TOP BUY/MARKET DEBUG."""
    try:
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        builder = getattr(dashboard, "_buy_gate_rows", None)
        if not builder:
            return []
        return builder(md, ws, meta, limit) or []
    except Exception:
        return []


def _display_status(r, watch_addresses):
    status = r.get("v25_status")
    address = str(r.get("address") or "").lower()
    if status == "HUNT":
        return "HUNT"
    if status == "WATCH":
        return "WATCHING" if address in watch_addresses else "WATCH CANDIDATE"
    return "REJECT"


def _gate_text(r):
    reasons = r.get("v25_gate_reasons") or []
    if not reasons:
        return "BUY GATES PASS"
    return "; ".join(str(x) for x in reasons[:3])


def _pump_section_debug(dashboard, text):
    rows = _canonical_rows(dashboard, 5)
    state = _state(dashboard)
    stats = state.get("stats", {}) if isinstance(state, dict) else {}
    watch = state.get("watch", {}) if isinstance(state, dict) else {}
    watch_addresses = {str(a).lower() for a in watch} if isinstance(watch, dict) else set()
    lines = [
        text,
        "",
        "🚀 PUMP-HUNTER • ACTIVE DIAGNOSTIC",
        "────────────────────────",
        f"Learning: {'READY' if stats.get('closed', 0) >= 20 or len(watch) >= 20 else 'WARMING UP'} • closed {stats.get('closed', 0)} • wins {stats.get('wins', 0)} • realized {stats.get('realized_pnl_sda', 0):+.2f} SDA",
        f"Open pump positions: {len(state.get('positions', {}) or {})} • WATCH: {len(watch_addresses)}",
    ]
    if rows:
        lines.append("")
        lines.append("Same 5 canonical V25 candidates as TOP BUY CANDIDATES:")
        for i, r in enumerate(rows, 1):
            status = _display_status(r, watch_addresses)
            icon = {"HUNT": "🟣 V25 HUNT", "WATCHING": "🟡 V25 WATCHING", "WATCH CANDIDATE": "🟡 V25 WATCH CANDIDATE", "REJECT": "⚪ V25 REJECT"}[status]
            d = r.get("decision") or {}
            v = r.get("v25") or {}
            score = _num(r.get("score"))
            volume = _num(r.get("v25_volume_1h"))
            ev = _num(r.get("v25_expected_pl"))
            mfe = _num(r.get("v25_expected_mfe"))
            mae = _num(r.get("v25_expected_mae"))
            p10 = _num(r.get("v25_p10"))
            p20 = _num(r.get("v25_p20"))
            p30 = _num(r.get("v25_p30"))
            reasons = r.get("v25_gate_reasons") or []
            lines += [
                f"{i}. {icon} • {'🟢 CORE' if not d.get('blocked') else '🔴 CORE BLOCK'} • {r.get('label', '?')} • score {score:.0f}/100",
                f"   VOL 1H {volume:.0f} SDA / 250 SDA • EV {ev:+.2f} SDA • MFE {mfe:+.1f}% • MAE {mae:+.1f}% • P10/P20/P30 {p10:.0%}/{p20:.0%}/{p30:.0%}",
                f"   WHY NOT BUY: {_gate_text(r)}" if reasons else "   BUY GATES PASS",
            ]
    else:
        lines.append("⚪ No V25 candidates")
    return "\n".join(lines)


def _pump_section_actions(dashboard, text):
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
            c = hunter._features(scanner, str(address).lower(), an, ws) if an else {"pump_score":0,"m1h":0,"flow":0}
            peak = _num(pos.get("peak_price"), entry)
            peak_roi = (peak / entry - 1.0) * 100.0 if entry > 0 else 0.0
            trailing = peak * (1.0 - hunter._trail({"entry_price": entry, "peak_price": peak}, c)) if peak > 0 else 0.0
            if price > 0 and peak_roi >= 12 and price <= trailing:
                action, icon = "PUMP TRAILING EXIT", "🔴"
            elif roi >= 10 and c.get("m1h", 0) < -5 and c.get("m15", 0) < -3 and c.get("flow", 0) < 0:
                action, icon = "PUMP REVERSAL EXIT", "🔴"
            elif roi > 0 and (c.get("m1h", 0) >= 0 or c.get("flow", 0) > 0):
                action, icon = "PUMP HOLD / TRAIL", "🟢"
            elif roi < -7:
                action, icon = "PUMP RISK", "🟠"
            else:
                action, icon = "PUMP HOLD / WATCH", "🟡"
            rows.append({"symbol": pos.get("symbol") or address, "roi": roi, "peak_roi": peak_roi, "score": c.get("pump_score", 0), "action": action, "icon": icon, "m1h": c.get("m1h", 0), "flow": c.get("flow", 0)})
        lines = [text, "", "🚀 PUMP-HUNTER • POSITION ACTION", "────────────────────────"]
        if not rows:
            lines.append("⚪ No open pump positions")
        else:
            for r in sorted(rows, key=lambda x: x["roi"], reverse=True):
                lines += [f"{r['icon']} {r['symbol']}: {r['action']} • P/L {r['roi']:+.2f}% • pump {r['score']:.0f}/100", f"   peak {r['peak_roi']:+.1f}% • M1H {r['m1h']:+.1f}% • flow {r['flow']:+.0f}"]
        return "\n".join(lines)
    except Exception:
        return text + "\n\n🚀 PUMP-HUNTER • POSITION ACTION\n────────────────────────\n⚪ No open pump positions"


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_pump_dashboard_patched", False):
        return dashboard
    original_debug = dashboard.market_debug_report
    original_real = dashboard.real_trading_report

    # Do not wrap top_buy: dashboard_v23_patch already owns the canonical
    # V25-aware TOP BUY CANDIDATES list. A second independent list was the
    # source of the confusing REG[S] vs sUSDT mismatch.
    def market_debug_report(*args, **kwargs):
        return _pump_section_debug(dashboard, original_debug(*args, **kwargs))

    def real_trading_report(*args, **kwargs):
        return _pump_section_actions(dashboard, original_real(*args, **kwargs))

    dashboard.market_debug_report = market_debug_report
    dashboard.real_trading_report = real_trading_report
    dashboard._sda_pump_dashboard_patched = True
    return dashboard
