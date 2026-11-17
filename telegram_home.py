"""Concise, read-only Telegram home screen and independent REAL BUY Scanner.

Presentation only: reads REAL accounting and REAL BUY signal diagnostics.
Never prepares, signs or broadcasts a transaction.
"""
from datetime import datetime, timezone
from decimal import Decimal

import real_bot_menu as rb
import real_bot_views
import real_trading_config as cfg
import real_wallet_reconciliation as wallet_reconciliation
import v30_dashboard_patch as v30_view

SCANNER_LIMIT = 5
# Same freshness window the shared signal engine applies to entry data.
SCANNER_STALE_SECONDS = float(v30_view.v30.MAX_ENTRY_DATA_AGE_SEC)
STATUS_ICONS = {"READY": "🟢", "NO SIGNAL": "🟡", "PENDING": "🟡", "BLOCKED": "🔴",
                "DATA STALE": "⚪", "UNKNOWN": "⚪"}


class ScannerRows(list):
    """Ranked shared-signal rows plus the scanner snapshot timestamp."""
    updated_at = None


def _n(value, default=0.0):
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _status(decision):
    if not decision.get("paper_buy_blocked"):
        return "BUY"
    return "WATCH" if str(decision.get("pump_phase") or "NO") == "WATCH" else "NO"


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
    ranked = ScannerRows()
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
    ranked.updated_at = md.get("updated_at")
    return ranked


def _counts(rows):
    ready = sum(1 for row in rows if row["status"] == "BUY")
    watch = sum(1 for row in rows if row["status"] == "WATCH")
    return ready, watch


def _scanner_stale(rows, now=None):
    try:
        stamp = datetime.fromisoformat(str(rows.updated_at).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        return True
    age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    return age > SCANNER_STALE_SECONDS


def _sda(value):
    text = f"{value:.2f}"
    return (text[:-3] if text.endswith(".00") else text) + " SDA"


def _held_tokens(positions):
    return {str(k).lower() for k in positions} | {
        str(v.get("token")).lower() for v in positions.values() if isinstance(v, dict) and v.get("token")}


def real_capacity(dashboard):
    """Single admission view from the latest blockchain accounting snapshot."""
    worker = rb._load_worker_status(dashboard) or {}
    try:
        accounting = dashboard.load("real_accounting_state.json", {})
    except Exception:
        return None
    if not isinstance(accounting, dict) or accounting.get("bot_reconciliation_status") != "COMPLETE":
        return None
    sync_stamp = worker.get("lastSuccessfulWalletSync") or accounting.get("generated_at")
    try:
        stamp = datetime.fromisoformat(str(sync_stamp).replace("Z", "+00:00"))
        if stamp.tzinfo is None or (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() > wallet_reconciliation.MAX_SNAPSHOT_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    if worker.get("syncStatus") != "FRESH" or "STATUS UNKNOWN" in rb._status(worker):
        return None
    state = rb._state(dashboard)
    try:
        metadata = dashboard.load("token_metadata.json", {})
    except Exception:
        metadata = {}
    accounting_positions = {}
    deployed = Decimal("0")
    for item in accounting.get("open_positions") or []:
        if not isinstance(item, dict) or not item.get("token"):
            continue
        token = str(item["token"]).lower()
        try:
            cost = Decimal(str(item.get("cost_basis_sda")))
            quantity = Decimal(str(item.get("quantity")))
            if cost <= 0 or quantity <= 0:
                continue
        except Exception:
            continue
        accounting_positions[token] = item
        deployed += cost
    pending = {k: v for k, v in (state.get("pending_buys") or {}).items() if isinstance(v, dict)}
    pending_cost = sum((Decimal(str(v.get("position_sda") or 0)) for v in pending.values()), Decimal("0"))
    deployed += pending_cost
    cap = Decimal(str(worker.get("real_capital_cap_sda") or cfg.TRADING_WALLET_CAPITAL_SDA))
    native = Decimal(str(accounting.get("native_balance_sda") or 0))
    available = max(Decimal("0"), min(cap - deployed, native))
    return {
        "positions": len(accounting_positions), "reserved_positions": len(accounting_positions) + len(pending),
        "pending": len(pending),
        "max_positions": int(worker.get("real_max_open_positions") or cfg.MAX_OPEN_POSITIONS),
        "cap": float(cap), "deployed": float(deployed), "available": float(available),
        "next_buy": float(worker.get("real_buy_size_sda") or cfg.position_sda()),
        "held": set(accounting_positions) | _held_tokens(pending),
        "sync_status": worker.get("syncStatus"),
    }

def real_gate(real, token=None):
    """(state, reason) for one BUY-ready signal under current REAL (PAPER-coupled) admission."""
    if real is None:
        return "UNKNOWN", "REAL state unavailable"
    if real["pending"]:
        return "PENDING", "BUY in flight"
    token = str(token or "").lower()
    if token and token in real["held"]:
        return "BLOCKED", "REAL token already held or pending"
    if real["reserved_positions"] >= real["max_positions"]:
        return "BLOCKED", "Position limit"
    if real["deployed"] + real["next_buy"] > real["cap"] + 1e-9:
        return "BLOCKED", "Capital limit"
    return "READY", None


def real_buy_status(rows, real, stale):
    """(state, reason, best BUY-ready row) for the HOME summary."""
    if stale:
        return "DATA STALE", None, None
    ready = [row for row in rows if row["status"] == "BUY"]
    if real is not None and real["pending"]:
        return "PENDING", "BUY in flight", ready[0] if ready else None
    if not ready:
        return "NO SIGNAL", None, None
    gates = [(row, real_gate(real, row["address"])) for row in ready]
    for row, (state, reason) in gates:
        if state == "READY":
            return state, reason, row
    row, (state, reason) = gates[0]
    return state, reason, row


def _real_lines(real, labelled=True):
    if real is None:
        return ["⚪ REAL capacity unavailable"]
    if labelled:
        return [_line("💰 Available", _sda(real["available"])),
                _line("💵 Next BUY", _sda(real["next_buy"]))]
    return [f"Available: {_sda(real['available'])}",
            f"Next BUY: {_sda(real['next_buy'])}"]


def _metrics(decision):
    return f"15m {_n(decision.get('m15')):+.1f}% | Flow {_n(decision.get('net_1h')):+.0f} SDA"


def _scanner_inputs(dashboard):
    # REAL scanner freshness is market-source freshness only. Never invoke the
    # PAPER V30 decision engine to decide or count REAL BUY-ready candidates.
    market_snapshot = dashboard.load("market_data.json", {})
    rows = ScannerRows()
    rows.updated_at = market_snapshot.get("updated_at") if isinstance(market_snapshot, dict) else None
    try:
        real = real_capacity(dashboard)
    except Exception as exc:
        print(f"REAL capacity error: {type(exc).__name__}: {exc}")
        real = None
    whale_state = dashboard.load("whale_state.json", {})
    whale_stamp = ((whale_state or {}).get("last_successful_whale_at")
                   or (whale_state or {}).get("updated_at")) if isinstance(whale_state, dict) else None
    whale_rows = ScannerRows()
    whale_rows.updated_at = whale_stamp
    whale_stale = (not isinstance(whale_state, dict)
                   or whale_state.get("whale_source_health") == "SOURCE_UNAVAILABLE"
                   or _scanner_stale(whale_rows))
    market_state = dashboard.load("market_source_state.json", {})
    market_stamp = ((market_state or {}).get("last_successful_market_at")
                    or (market_state or {}).get("updated_at")) if isinstance(market_state, dict) else None
    market_rows = ScannerRows()
    market_rows.updated_at = market_stamp
    worker = rb._load_worker_status(dashboard) or {}
    source_health = (worker.get("market_source_health")
                     or (worker.get("data_source_health") or {}).get("market"))
    market_stale = (isinstance(market_state, dict)
                    and (market_state.get("source_health") == "SOURCE_UNAVAILABLE"
                         or (market_stamp is not None and _scanner_stale(market_rows))))
    market_stale = market_stale or source_health in ("STALE", "SOURCE_UNAVAILABLE")
    wallet_sync_stale = (worker.get("syncStatus") != "FRESH"
                         or "STATUS UNKNOWN" in rb._status(worker)
                         or "CRITICAL FAILURE" in str(worker.get("worker_health") or ""))
    return rows, real, _scanner_stale(rows) or whale_stale or market_stale or wallet_sync_stale


def _home_scanner_lines(dashboard):
    rows, real, stale = _scanner_inputs(dashboard)
    real_signal = dashboard.load("real_buy_signal_state.json", {})
    last_scan = real_signal.get("last_scan_at") if isinstance(real_signal, dict) else None
    try:
        signal_stamp = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00"))
        signal_age = (datetime.now(timezone.utc) - signal_stamp.astimezone(timezone.utc)).total_seconds() if signal_stamp.tzinfo else float("inf")
    except (TypeError, ValueError):
        signal_age = float("inf")
    stale = stale or signal_age < 0 or signal_age > 300
    worker = rb._load_worker_status(dashboard) or {}
    lines = ["", "🔥 REAL BUY SCANNER"]
    if stale:
        lines.append("⚪ Data stale")
        if _scanner_stale(rows):
            lines.append("⚠ Scanner rows stale")
        for key, label in (("whale_source_health", "Whale source"),
                           ("market_source_health", "Market source")):
            source_health = worker.get(key)
            if source_health and source_health not in {"HEALTHY", "ACTIVE", "FRESH", "OK"}:
                lines.append(f"⚠ {label}: {source_health}")
        if worker.get("syncStatus") != "FRESH" or "STATUS UNKNOWN" in rb._status(worker):
            detail = worker.get("lastSyncError") or rb._status(worker)
            lines.append(f"⚠ {detail}")
            last_sync = worker.get("lastSuccessfulWalletSync")
            if last_sync:
                lines.append(f"Last successful wallet sync: {last_sync}")
    else:
        summary = real_signal.get("summary", {}) if isinstance(real_signal, dict) else {}
        lines += [_line("🟢 BUY-ready", str(int(summary.get("buy_ready") or 0))),
                  _line("🟡 WATCH", str(int(summary.get("watch") or 0)))]
    lines += _real_lines(real)
    summary = real_signal.get("summary", {}) if isinstance(real_signal, dict) else {}
    state = "READY" if int(summary.get("buy_ready") or 0) > 0 else ("DATA STALE" if stale else "NO SIGNAL")
    lines.append(f"{STATUS_ICONS[state]} REAL BUY: {state}")
    diagnostics = real_signal.get("last_diagnostics", {}) if isinstance(real_signal, dict) else {}
    best = next((item for item in diagnostics.values() if isinstance(item, dict) and item.get("buy_ready")), None)
    if best:
        lines += [f"🎯 {best.get('symbol') or 'Candidate'}  {_n(best.get('score')):.0f}/100",
                  f"   {best.get('phase') or 'NO'} | Age {_n(best.get('data_age_sec')):.0f}s"]
    return lines


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
        if "CRITICAL FAILURE" in status:
            lines.append("🔴 REAL Trading CRITICAL FAILURE")
        elif "BUY DATA DEGRADED" in status:
            lines.append("🟠 REAL Trading BUY DATA DEGRADED")
        elif "LIVE" in status:
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
            _line("📦 Positions", f"{len(data.get('positions') or [])}/{int(data.get('max_positions') or 10)}"),
        ]
    try:
        lines += _home_scanner_lines(dashboard)
    except Exception as exc:
        print(f"Home scanner summary error: {type(exc).__name__}: {exc}")
        lines += ["", "🔥 REAL BUY SCANNER", "⚪ Scanner data unavailable"]
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
    real = real_capacity(dashboard)
    signal_state = dashboard.load("real_buy_signal_state.json", {})
    summary = signal_state.get("summary", {}) if isinstance(signal_state, dict) else {}
    diagnostics = signal_state.get("last_diagnostics", {}) if isinstance(signal_state, dict) else {}
    lines = ["🔍 REAL BUY SCANNER • DETAIL" if detail else "🔥 REAL BUY SCANNER", ""]
    last_scan = signal_state.get("last_scan_at") if isinstance(signal_state, dict) else None
    try:
        signal_stamp = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00"))
        signal_age = (datetime.now(timezone.utc) - signal_stamp.astimezone(timezone.utc)).total_seconds() if signal_stamp.tzinfo else float("inf")
    except (TypeError, ValueError):
        signal_age = float("inf")
    if signal_age < 0 or signal_age > 300:
        lines.append("⚪ REAL BUY signal data stale or unavailable")
    lines += [
        _line("🟢 BUY-ready", str(int(summary.get("buy_ready") or 0))),
        _line("🟡 WATCH", str(int(summary.get("watch") or 0))),
    ]
    lines += _real_lines(real, labelled=False)
    lines.append(f"📦 Positions: {int(summary.get('positions') or 0)}/{int(summary.get('max_positions') or cfg.MAX_OPEN_POSITIONS)}")
    if not diagnostics:
        lines.append("⚪ REAL scanner diagnostics unavailable")
        return "\n".join(lines)
    ranked = sorted(diagnostics.items(), key=lambda item: (
        not bool(item[1].get("buy_ready")), -_n(item[1].get("score"))
    ))
    for index, (token, item) in enumerate(ranked[:SCANNER_LIMIT], 1):
        status = "🟢 BUY-ready" if item.get("buy_ready") else "🟡 WATCH"
        lines.append(f"{index}. {item.get('symbol') or token[:10]} {status} {_n(item.get('score')):.0f}/100")
        lines.append(f"   {item.get('phase') or 'NO'} | Age {_n(item.get('data_age_sec')):.0f}s")
        if not item.get("buy_ready"):
            lines.append(f"   REAL blocker: {item.get('blocker') or 'unknown'}")
        if detail:
            lines.append(f"   Quote: {item.get('quote_at') or 'N/A'}")
            temporal = item.get("temporal") or {}
            lines.append(f"   Temporal: {temporal.get('reason') or 'N/A'}")
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
