"""Concise, read-only Telegram home screen and BUY Scanner.

Presentation only: reuses the REAL accounting view model and the PAPER V30
decision engine. Never prepares, signs or broadcasts a transaction.
"""
import real_bot_views
import v30_dashboard_patch as v30_view

SCANNER_LIMIT = 5


def _n(value, default=0.0):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _status(decision):
    phase = str(decision.get("pump_phase") or "NO")
    if not decision.get("paper_buy_blocked") and phase in ("IGNITION", "CONFIRMATION", "BREAKOUT"):
        return "BUY"
    return "WATCH" if phase == "WATCH" else "NO"


def _symbol(dashboard, address, meta):
    item = meta.get(str(address).lower()) if isinstance(meta, dict) else None
    if isinstance(item, dict) and item.get("symbol"):
        return str(item["symbol"])
    try:
        return str(dashboard.engine.lbl(address, meta)).split("/")[0]
    except Exception:
        return str(address)[:10]


def scanner_candidates(dashboard):
    """Rank PAPER candidates exactly like the former TOP BUY section."""
    md = v30_view._merged_market(dashboard, dashboard.load("market_data.json", {"tokens": {}}))
    ws = dashboard.load("whale_data.json", {})
    meta = dashboard.load("token_metadata.json", {})
    ranked = []
    for address, token_data in (md.get("tokens", {}) or {}).items():
        analysis = v30_view._analysis(token_data)
        if not analysis or _n(analysis.get("price_in_sda")) <= 0:
            continue
        decision = v30_view._decision(dashboard, address, analysis, ws)
        ranked.append({"address": str(address), "symbol": _symbol(dashboard, address, meta),
                       "status": _status(decision), "decision": decision})
    ranked.sort(key=lambda row: (
        1 if row["decision"].get("shadow_confirmation_ready") else 0,
        1 if not row["decision"].get("paper_buy_blocked") else 0,
        1 if row["decision"].get("data_fresh_for_entry") else 0,
        _n(row["decision"].get("pump_score")),
        _n(row["decision"].get("shadow_snapshot_impulse")),
        _n(row["decision"].get("pump_change")),
    ), reverse=True)
    return ranked


def _counts(rows):
    ready = sum(1 for row in rows if not row["decision"].get("paper_buy_blocked"))
    watch = sum(1 for row in rows if row["status"] == "WATCH")
    return ready, watch


def _line(label, value, width=14):
    return f"{label:<{width}}{value}"


def home_text(dashboard):
    lines = ["🤖 SDA TRADING BOT", ""]
    try:
        data = real_bot_views._get_data(dashboard)
    except Exception as exc:
        print(f"Home REAL data error: {type(exc).__name__}: {exc}")
        data = None
    if not data or data.get("source") == "unavailable":
        lines.append("⚪ REAL Trading data unavailable")
    else:
        status = str(data.get("worker_status") or "")
        if "LIVE" in status:
            lines.append("🟢 REAL Trading LIVE")
        elif "DRY" in status:
            lines.append("🟡 REAL Trading DRY RUN")
        else:
            lines.append(f"🔴 REAL Trading {status.split(' ', 1)[-1] if status else 'UNKNOWN'}")
        lines.append("")
        portfolio = data.get("portfolio_value")
        if portfolio is None:
            portfolio_text = "N/A"
        else:
            portfolio_text = ("" if data.get("portfolio_complete") else "≥") + f"{portfolio:.2f} SDA"
        partial = " ⚠" if data.get("total_partial") else ""
        lines += [
            _line("💰 Portfolio", portfolio_text),
            _line("📈 Bot PnL", real_bot_views._signed_sda(data.get("total_pnl")) + partial),
            _line("📊 Open PnL", real_bot_views._signed_sda(data.get("open_pnl"))),
            _line("💵 Realized", real_bot_views._signed_sda(data.get("realized_pnl"))),
            _line("📦 Positions", str(len(data.get("positions") or []))),
        ]
    try:
        ready, watch = _counts(scanner_candidates(dashboard))
        lines += ["", "🧪 PAPER", _line("BUY-ready", str(ready)), _line("WATCH", str(watch))]
    except Exception as exc:
        print(f"Home scanner summary error: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


def home_keyboard():
    return {"inline_keyboard": [
        [{"text": "🤖 REAL Trading", "callback_data": "REAL_BOT"}],
        [{"text": "🔥 BUY Scanner", "callback_data": "BUY_SCANNER"},
         {"text": "📈 PnL statistika", "callback_data": "REAL_BOT_PNL"}],
        [{"text": "🧪 PAPER", "callback_data": "PAPER"},
         {"text": "🧠 AI Optimizer", "callback_data": "OPTIMIZER"}],
        [{"text": "🔄 Refresh", "callback_data": "REFRESH"}],
    ]}


def buy_scanner_text(dashboard, detail=False):
    rows = scanner_candidates(dashboard)
    ready, watch = _counts(rows)
    lines = ["🔍 BUY SCANNER • DETAIL" if detail else "🔥 BUY SCANNER", "",
             f"BUY-ready: {ready}", f"WATCH: {watch}", ""]
    if not rows:
        lines.append("⚪ No market candidates")
    for index, row in enumerate(rows[:SCANNER_LIMIT], 1):
        d = row["decision"]
        lines.append(f"{index}. {row['symbol']:<6} {row['status']:<5} {_n(d.get('pump_score')):.0f}/100")
        lines.append(f"   15m {_n(d.get('m15')):+.1f}% | Flow {_n(d.get('net_1h')):+.0f} SDA")
        if detail:
            lines.append(f"   1h {_n(d.get('m1h')):+.1f}% | Vol {_n(d.get('volume_1h')):.0f} | "
                         f"Trades {_n(d.get('trades_1h')):.0f} | Ratio {_n(d.get('buy_ratio')):.2f}x")
            lines.append(f"   Phase {d.get('pump_phase') or 'NO'} | "
                         f"{d.get('paper_buy_block_reason') or 'ready'}")
    return "\n".join(lines)


def buy_scanner_keyboard(detail=False):
    first = ([{"text": "🔥 BUY Scanner", "callback_data": "BUY_SCANNER"}] if detail
             else [{"text": "🔍 Detail", "callback_data": "BUY_SCANNER_DETAIL"}])
    return {"inline_keyboard": [
        first + [{"text": "🔄 Refresh", "callback_data": "BUY_SCANNER"}],
        [{"text": "⬅️ Back", "callback_data": "MAIN"}],
    ]}


def install(dashboard):
    """Replace the verbose legacy home with the concise home; keep the legacy renderer for debugging."""
    if getattr(dashboard, "_sda_home_installed", False):
        return dashboard
    dashboard.legacy_main_dashboard = getattr(dashboard, "main_dashboard", None)
    dashboard.main_dashboard = lambda: home_text(dashboard)
    dashboard.menu_keyboard = home_keyboard
    dashboard._sda_home_installed = True
    return dashboard
