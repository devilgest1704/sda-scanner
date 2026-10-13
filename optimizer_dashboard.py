"""Read-only Telegram statistics for the autonomous and research PAPER optimizers.

Render from committed GitHub reports. On Vercel, fail closed if the remote
report is unavailable: never disguise a frozen deployment copy as live data.
No workflow dispatch, execution, config writes, or real-wallet actions.
"""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os
import time

import urllib.error
import urllib.request

BASE = "https://raw.githubusercontent.com/devilgest1704/sda-scanner/main/"
AUTO = "strategy_lab/autonomous_agent_report.json"
CONFIG = "v30_agent_config.json"
RESEARCH = "v30_ai_optimizer.json"
ALLOWED = frozenset((AUTO, CONFIG, RESEARCH))
PRAGUE = ZoneInfo("Europe/Prague")

STATUSES = {
    "COLLECTING_FORWARD_TRADES": "Sbírá ověřené forward obchody",
    "WAITING_FOR_FRESH_SCANNER": "Čeká na aktuální data scanneru",
    "NO_PROFITABLE_TRAIN_CHALLENGER": "Zatím žádný ziskový kandidát",
    "HOLDOUT_REJECTED": "Kandidát neprošel nezávislým testem",
    "PROMOTED_PAPER": "Nový paper Champion nasazen",
    "ROLLED_BACK_PAPER": "Vrácena předchozí paper strategie",
    "MONITORING_PAPER_PROMOTION": "Sleduje nového paper Championa",
    "WAITING_FOR_NEW_EVIDENCE": "Čeká na nové obchody",
    "CHAMPION_ALREADY_SELECTED": "Champion se nemění",
    "UNKNOWN_INCUMBENT_POLICY": "Aktivní strategii nelze bezpečně ověřit",
    "RUNTIME_PARITY_UNVERIFIED": "Výstupy Shadow a V30 nejsou dosud srovnatelné",
}


def load_report(path):
    """Read latest committed data. None means missing; not 'zero performance'."""
    if path not in ALLOWED:
        raise ValueError("Unexpected optimizer report path")
    if os.environ.get("SDA_REMOTE_STATE") == "1":
        try:
            url = BASE + path + "?ts=" + str(int(time.time()))
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                      "User-Agent": "SDA-Optimizer-Dashboard"})
            with urllib.request.urlopen(req, timeout=7) as response:
                # Read-only reports are small; bound the payload to 256 KiB.
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ValueError("Optimizer report exceeds safe size")
            result = json.loads(raw.decode("utf-8"))
            return result if isinstance(result, dict) else None
        except (OSError, ValueError) as exc:
            print(f"Optimizer report unavailable {path}: {type(exc).__name__}")
            return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if abs(number) < 1e15 and number == number else default
    except (TypeError, ValueError, OverflowError):
        return default


def _time(value):
    if not isinstance(value, str) or not value:
        return "nedostupný"
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if t.tzinfo is None:
            return "nedostupný"
        return t.astimezone(PRAGUE).strftime("%d.%m. %H:%M") + " (Praha)"
    except (ValueError, OverflowError):
        return "nedostupný"


def _stale_notice(value, now=None):
    if not isinstance(value, str):
        return "⚠️ Datum vyhodnocení není k dispozici."
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Naive timestamp")
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        if age > 5400:
            return "⚠️ Výsledky vyhodnocení jsou starší než 90 minut."
    except (ValueError, OverflowError):
        return "⚠️ Datum vyhodnocení není platné."
    return ""


def _pnl(value):
    return f"{_number(value):+.2f} SDA"


def _filters(value):
    f = value if isinstance(value, dict) else {}
    if not f:
        return "Nedostupné"
    return (f"score ≥ {_number(f.get('entry_score')):g} • "
            f"quality ≥ {_number(f.get('quality')):g} • "
            f"impulse ≥ {_number(f.get('impulse')):g}\n"
            f"ratio ≥ {_number(f.get('buy_ratio')):.2f}x • "
            f"trades ≥ {_number(f.get('trades')):g}")


def overview_report(auto, config):
    lines = ["🧠 AI OPTIMIZER • AUTONOMNÍ UČENÍ", "",
             "🧪 PAPER ONLY • Virtuální obchody, nikoli reálné plnění",
             "────────────────────────"]
    if not isinstance(auto, dict):
        return "\n".join(lines + [
            "⚠️ Aktuální report autonomního agenta nelze načíst.",
            "Zastaralou kopii z Vercelu nezobrazuji jako živý výsledek.",
        ])
    st = str(auto.get("status") or "UNKNOWN")
    lines += [f"Stav: {STATUSES.get(st, st.replace('_', ' ').title())}",
              f"Vyhodnoceno: {_time(auto.get('updated_at'))}"]
    stale = _stale_notice(auto.get("updated_at"))
    if stale:
        lines.append(stale)
    lines.append(f"Data scanneru: {_time(auto.get('source_last_scan_at'))}")
    championship = (auto.get("champion") or {})
    cfg = config if isinstance(config, dict) else championship
    generation = cfg.get("generation", championship.get("generation", 0))
    lines += ["", f"🏆 CHAMPION • generace {generation}",
              _filters(cfg.get("filters") or championship.get("filters")),
              "────────────────────────"]
    strategies = auto.get("strategies") if isinstance(auto.get("strategies"), dict) else {}
    quality = auto.get("learning_data_quality") or {}
    if not isinstance(quality, dict):
        quality = {}
    tracked = len(strategies)
    baseline = (strategies.get("baseline") or {}).get("all") or {}
    q = quality.get("baseline") or {}
    lines += [f"Testované strategie: {tracked} (včetně WATCH/MACD/Breakout/Trend Runner výzkumu)",
              f"Baseline validních uzavření: {q.get('valid_closed', baseline.get('closed', 0))}",
              f"Vyřazená uzavření: {q.get('excluded_closed', 'N/A')}",
              f"Baseline shadow P/L: {_pnl(baseline.get('net_sda'))}",
              f"Nový Champion: {'ANO' if auto.get('promotion_applied') else 'NE'}",
              f"Rollback: {'ANO' if auto.get('rollback_applied') else 'NE'}"]
    selected = auto.get("selected_challenger") or {}
    if isinstance(selected, dict) and selected.get("name"):
        lines.append(f"Kandidát: {selected['name']}")
    reason = str(auto.get("reason") or "")
    if reason:
        lines += ["", "ℹ️ " + reason[:210]]
    lines += ["", "📌 K nasazení je nutný pozitivní train + holdout",
              "a splnění limitů kvality dat a drawdownu.",
              "Zobrazené P/L je simulace po odhadovaných nákladech."]
    return "\n".join(lines)


def strategies_report(auto):
    lines = ["🧪 AI OPTIMIZER • SHADOW STRATEGIE", "",
             "Virtuální výsledky po odhadovaných poplatcích a skluzu.",
             "Nesčítat s realizovaným P/L paper obchodování.",
             "────────────────────────"]
    if not isinstance(auto, dict):
        return "\n".join(lines + ["⚠️ Aktuální report strategií není dostupný."])
    lines += [f"Vyhodnoceno: {_time(auto.get('updated_at'))}", ""]
    strategies = auto.get("strategies") if isinstance(auto.get("strategies"), dict) else {}
    quality = auto.get("learning_data_quality") if isinstance(auto.get("learning_data_quality"), dict) else {}
    if not strategies:
        lines.append("Žádné statistiky strategií nejsou k dispozici.")
    for name, data in strategies.items():
        d = data.get("all") if isinstance(data, dict) else {}
        d = d if isinstance(d, dict) else {}
        q = quality.get(name) if isinstance(quality.get(name), dict) else {}
        count = int(_number(d.get("closed")))
        valid = int(_number(q.get("valid_closed", count)))
        excluded = int(_number(q.get("excluded_closed")))
        pnl = _number(d.get("net_sda"))
        icon = "⚪" if not count else ("🟢" if pnl > 0 else "🔴")
        tag = (" (V32 Trend Runner výzkum)" if name == "research_trend_runner"
               else " (V32 Breakout výzkum)" if name == "research_breakout_volume_trend"
               else " (V32 MACD výzkum)" if name.startswith("research_macd")
               else " (výzkum)" if name.startswith("research_") else "")
        lines += [f"{icon} {name}{tag}",
                  f"   {_pnl(pnl)} • {count} validních close • win {_number(d.get('win_rate_pct')):.1f} %",
                  f"   Celkem validních {valid} / vyřazeno {excluded} • PF {_number(d.get('profit_factor')):.2f}"]
    lines += ["", "⚠️ Malý vzorek neprokazuje ziskovost.",
              "WATCH, V32 MACD, Breakout ani Trend Runner se automaticky nenasazují."]
    return "\n".join(lines)


def research_report(report):
    lines = ["📈 V30 AI OPTIMIZER • VÝZKUM", "",
             "🧪 PAPER ONLY • Samostatný historický výzkum",
             "⚠️ Tento optimizer automaticky NENASAZUJE změny.",
             "────────────────────────"]
    if not isinstance(report, dict):
        return "\n".join(lines + ["⚠️ Výzkumný report není dostupný."])
    lines += [f"Vyhodnoceno: {_time(report.get('generated_at'))}",
              f"Status: {str(report.get('status') or 'UNKNOWN')}",
              f"Testováno variant: {int(_number(report.get('candidates_tested')))}",
              f"V30-kompatibilní data: {int(_number(report.get('v30_compatible_rows')))}"]
    champ = report.get("champion") or {}
    challenger = report.get("challenger") or {}
    champ_test = champ.get("test") if isinstance(champ, dict) else {}
    ch_test = challenger.get("test") if isinstance(challenger, dict) else {}
    champ_test = champ_test if isinstance(champ_test, dict) else {}
    ch_test = ch_test if isinstance(ch_test, dict) else {}
    lines += ["", "🏆 Současná strategie – test",
              f"   {_pnl(champ_test.get('pnl_sda'))} • {int(_number(champ_test.get('closed')))} uzavření",
              f"   PF {_number(champ_test.get('profit_factor')):.2f} • win {_number(champ_test.get('win_rate_pct')):.1f} %",
              "", "🧪 Nejlepší nalezený kandidát – test",
              f"   {_pnl(ch_test.get('pnl_sda'))} • {int(_number(ch_test.get('closed')))} uzavření",
              f"   PF {_number(ch_test.get('profit_factor')):.2f} • win {_number(ch_test.get('win_rate_pct')):.1f} %"]
    if isinstance(challenger, dict):
        cf = challenger.get("safe_runtime_filters") or {}
        if cf:
            lines += ["", "Navrhované filtry:", _filters(cf)]
        lines.append("Doporučení k nasazení: " +
                     ("ANO (jen doporučení)" if challenger.get("recommend_promotion") else "NE"))
    lines += ["", "Skutečně nasazeno: " + ("ANO" if report.get("promotion_applied") else "NE"),
              "Testovací P/L není záruka budoucího zisku."]
    return "\n".join(lines)


def current_overview():
    return overview_report(load_report(AUTO), load_report(CONFIG))


def current_strategies():
    return strategies_report(load_report(AUTO))


def current_research():
    return research_report(load_report(RESEARCH))
