"""Read-only Telegram screens for the PAPER Champion vs Challenger optimizer."""
import json
import os
import urllib.request
from pathlib import Path

REPORT = "strategy_lab/paper_optimizer_report.json"
RAW_BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"
MAX_TEXT = 3800
STATUS_LABELS = {"INSUFFICIENT_DATA": "WAITING FOR DATA", "WAITING_FOR_DATA": "WAITING FOR DATA",
                 "SHADOW": "SHADOW (forward test running)", "REJECTED_TRAIN": "REJECTED (train)",
                 "REJECTED_VALIDATION": "REJECTED (validation)", "REJECTED_HOLDOUT": "REJECTED (holdout)",
                 "RESEARCH_ONLY": "RESEARCH ONLY (SELL variant)", "PROMOTED": "PROMOTED TO PAPER",
                 "NO_CHALLENGER": "NONE"}


def load_report():
    if os.environ.get("VERCEL") == "1":
        try:
            with urllib.request.urlopen(RAW_BASE + REPORT, timeout=4) as response:
                value = json.loads(response.read().decode("utf-8"))
                if isinstance(value, dict):
                    return value
        except Exception:
            pass
    try:
        value = json.loads(Path(REPORT).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _sda(value):
    try:
        return f"{float(value):+.2f} SDA"
    except (TypeError, ValueError):
        return "N/A"


def _pf(m):
    if not m:
        return "N/A"
    if m.get("profit_factor_infinite"):
        return "∞"
    value = m.get("profit_factor")
    return "N/A" if value is None else f"{float(value):.2f}"


def _gate(value):
    return "✅" if value is True else ("❌" if value is False else "⏳")


def _describe(challenger):
    if not challenger:
        return "none"
    if challenger.get("kind") == "buy":
        return "BUY " + ", ".join(f"{k} {v}" for k, v in (challenger.get("buy_filters") or {}).items())
    params = challenger.get("sell_params") or {}
    return (f"SELL stop {params.get('emergency')}, partial {params.get('partial_size')}@{params.get('partial_trigger')}, "
            f"trail {params.get('trailing')}, TP {params.get('hard_tp_mode')}")


def _clip(text):
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rstrip() + "\n…"


def _unavailable(title):
    return f"{title}\n\n⚪ Optimizer report not available yet."


def overview(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("🧠 PAPER OPTIMIZER")
    champ = (r.get("champion") or {}).get("all") or {}
    chall = r.get("challenger") or {}
    chall_all = r.get("challenger_all") or {}
    gates = r.get("gates") or {}
    delta = (chall_all.get("net_pnl") - champ.get("net_pnl")) if chall_all and champ else None
    lines = ["🧠 PAPER OPTIMIZER", "",
             "🏆 Champion", f"Version: {(r.get('champion') or {}).get('version')} • Swing SELL V2",
             f"Trades: {champ.get('trades', 0)}", f"Net PnL: {_sda(champ.get('net_pnl'))}",
             f"Profit factor: {_pf(champ)}", f"Max DD: {_sda(-(champ.get('max_drawdown') or 0))}", "",
             "🧪 Challenger", f"Status: {STATUS_LABELS.get(r.get('status'), r.get('status'))}", f"Version: {_describe(chall)}",
             f"Trades: {chall_all.get('trades', 'N/A')}", f"Net PnL: {_sda(chall_all.get('net_pnl'))}",
             f"Δ vs Champion: {_sda(delta)}", "",
             "Gates",
             f"Train {_gate(gates.get('train'))}  Validation {_gate(gates.get('validation'))}  "
             f"Holdout {_gate(gates.get('holdout'))}",
             f"Shadow {_gate(gates.get('shadow'))}  Data quality {_gate(gates.get('data_quality'))}", "",
             f"Decision: {r.get('decision')}"]
    for reason in (r.get("reasons") or [])[:4]:
        lines.append(f"• {reason}")
    lines += ["", f"PAPER only • REAL never promoted • {str(r.get('generated_at') or '')[:16]}"]
    return _clip("\n".join(lines))


def _metric_lines(m):
    if not m:
        return ["N/A"]
    return [f"Trades {m.get('trades')} • tokens {m.get('tokens')}",
            f"Net {_sda(m.get('net_pnl'))} (realized {_sda(m.get('realized_pnl'))}, open {_sda(m.get('open_pnl'))})",
            f"PF {_pf(m)} • expectancy {_sda(m.get('expectancy'))} • win {m.get('win_rate')}%",
            f"Avg win {_sda(m.get('avg_winner'))} • avg loss {_sda(m.get('avg_loser'))}",
            f"Best {_sda(m.get('best_trade'))} • worst {_sda(m.get('worst_trade'))}",
            f"Max DD {_sda(-(m.get('max_drawdown') or 0))} • costs {_sda(m.get('modeled_costs'))}"]


def champion(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("🏆 PAPER CHAMPION")
    c = r.get("champion") or {}
    ds = r.get("dataset") or {}
    lines = ["🏆 PAPER CHAMPION", "", f"Version: {c.get('version')}",
             "SELL: shared Swing SELL V2 (-50% stop, 20% @ +25%, 15% trail, +50% TP, no time exit)",
             f"BUY filters: {c.get('buy_filters') or 'current PAPER BUY (no extra filters)'}", "",
             "All completed trades (replayed)", *_metric_lines(c.get("all")), "",
             f"Dataset {ds.get('closed_trades')} trades • {ds.get('tokens')} tokens • "
             f"{ds.get('complete_paths')} complete paths", f"{str(ds.get('from'))[:10]} → {str(ds.get('to'))[:10]}"]
    for name, split in (r.get("splits") or {}).items():
        m = (c.get("splits") or {}).get(name) or {}
        lines.append(f"{name}: {split.get('trades')} trades • net {_sda(m.get('net_pnl'))} • PF {_pf(m)}")
    return _clip("\n".join(lines))


def challenger(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("🧪 CHALLENGER")
    c = r.get("challenger")
    if not c:
        return "🧪 CHALLENGER\n\n⚪ No challenger yet."
    lines = ["🧪 CHALLENGER", "", f"Status: {r.get('status')}", f"Config: {_describe(c)}",
             f"Kind: {'BUY filter (PAPER promotable)' if c.get('kind') == 'buy' else 'SELL variant (research only)'}"]
    for name in ("train", "validation", "holdout"):
        m = c.get(name)
        gate = (c.get("gates") or {}).get(name)
        lines += ["", f"{name.title()} {_gate(gate)}"] + (_metric_lines(m) if m else ["not evaluated"])
    if c.get("fee_sensitivity") is not None:
        lines.append(f"Fee sensitivity: {c['fee_sensitivity']:.2f}")
    if c.get("reasons"):
        lines += ["", "Rejected because:"] + [f"• {x}" for x in c["reasons"][:6]]
    return _clip("\n".join(lines))


def comparison(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("📊 COMPARISON")
    lines = ["📊 CHAMPION vs CHALLENGER", f"({r.get('comparison_scope')})", "",
             "Metric | Champion | Challenger | Δ"]
    for row in r.get("comparison") or []:
        def fmt(value):
            return "N/A" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))
        lines.append(f"{row['metric']} | {fmt(row['champion'])} | {fmt(row['challenger'])} | {fmt(row['difference'])}")
    if not r.get("challenger_all"):
        lines += ["", "No comparable challenger data: no promotion."]
    return _clip("\n".join(lines))


def research(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("🔬 RESEARCH")
    res = r.get("research") or {}
    lines = ["🔬 RESEARCH", "",
             f"Challengers tested: {r.get('candidates_tested', 0)}",
             f"Passed train: {r.get('candidates_passed_train', 0)} • validation: {r.get('candidates_passed_validation', 0)}",
             f"Shadow running: {len(r.get('shadow') or [])}", "",
             "Missed-pump research (observational)",
             f"Status: {res.get('status')}", f"Observed candidates: {res.get('observed_candidates')}",
             f"Excluded inputs: {res.get('excluded_inputs')}", f"Missed pumps: {res.get('missed_pump_examples')}",
             f"240m peaks: {res.get('peak_240m_examples')}", f"Rules tested: {res.get('tested_rules')}"]
    for item in (r.get("shadow") or [])[:3]:
        lines.append(f"Shadow {item.get('candidate_id')}: {item['challenger'].get('trades')} trades, "
                     f"{item.get('days')} d • {'PASS' if item.get('passed') else 'waiting'}")
    if r.get("lane_research"):
        from tools import lane_research
        lines += ["", lane_research.render_text(r["lane_research"])]
    return _clip("\n".join(lines))


def rollback(report=None):
    r = report if report is not None else load_report()
    if not r:
        return _unavailable("↩️ ROLLBACK")
    rb = r.get("rollback") or {}
    lines = ["↩️ PAPER ROLLBACK", "",
             f"Previous champion: {rb.get('previous_version') or 'none (baseline active)'}",
             f"Rollback ready: {'YES' if rb.get('ready') else 'NO (nothing to restore)'}",
             f"Trigger active: {'YES' if rb.get('triggered') else 'NO'}",
             f"Applied this run: {'YES' if r.get('rollback_applied') else 'NO'}"]
    lines += [f"• {x}" for x in rb.get("reasons") or []]
    lines += ["", "Rollback restores the previous PAPER champion only.",
              "REAL Swing SELL V2 and open REAL positions are never changed.",
              "This screen is read-only."]
    return "\n".join(lines)


def keyboard():
    return {"inline_keyboard": [
        [{"text": "🏆 Champion", "callback_data": "OPT_CHAMPION"}, {"text": "🧪 Challenger", "callback_data": "OPT_CHALLENGER"}],
        [{"text": "📊 Comparison", "callback_data": "OPT_COMPARE"}, {"text": "🔬 Research", "callback_data": "OPT_RESEARCH"}],
        [{"text": "↩️ Rollback", "callback_data": "OPT_ROLLBACK"}],
        [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}],
    ]}


def install(dashboard):
    dashboard.optimizer_overview_report = overview
    dashboard.optimizer_champion_report = champion
    dashboard.optimizer_challenger_report = challenger
    dashboard.optimizer_comparison_report = comparison
    dashboard.optimizer_paper_research_report = research
    dashboard.optimizer_rollback_report = rollback
    dashboard.optimizer_menu_keyboard = keyboard
    return dashboard
