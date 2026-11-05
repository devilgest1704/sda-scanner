import json, os, requests
from datetime import datetime, timezone
import engine_legacy as engine

POSITIONS_FILE = "positions.json"
MARKET_FILE = "market_data.json"
STATS_FILE = "paper_stats.json"
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default


def num(v, d=0.0):
    try: return d if v is None else float(v)
    except Exception: return d


def send(text):
    print(text)
    if TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": text}, timeout=30)
        except Exception as e:
            print("Telegram error:", e)


def main():
    p = load(POSITIONS_FILE, {"positions": {}, "closed_trades": []})
    md = load(MARKET_FILE, {"tokens": {}})
    positions = p.get("positions", {}) or {}
    closed = p.get("closed_trades", []) or []
    tokens = md.get("tokens", {}) or {}

    realized = sum(num(x.get("closed_profit_sda")) for x in closed)
    invested_closed = sum(num(x.get("investment_sda")) * num(x.get("closed_fraction", 1)) for x in closed)
    wins = sum(1 for x in closed if num(x.get("closed_profit_sda")) > 0)
    losses = sum(1 for x in closed if num(x.get("closed_profit_sda")) < 0)

    open_pnl = 0.0
    open_invested = 0.0
    open_rows = []
    stale_open_quotes = 0
    observed = datetime.now(timezone.utc)
    for address, pos in positions.items():
        token = str(address).lower()
        td = tokens.get(token, {}) if isinstance(tokens, dict) else {}
        an = td.get("analysis", td) if isinstance(td, dict) else {}
        cur = num(an.get("price_in_sda"))
        entry = num(pos.get("entry_price"))
        fraction = num(pos.get("remaining_fraction", 1))
        investment = num(pos.get("investment_sda")) * fraction
        try:
            quoted = datetime.fromisoformat(str(an.get("last_transaction")).replace("Z", "+00:00"))
            age = (observed-quoted).total_seconds()
            stale_open_quotes += not 0 <= age <= 300
        except (ValueError, TypeError):
            stale_open_quotes += 1
        if cur > 0 and entry > 0:
            # Match actual PAPER close(): both fees plus exit slippage.
            # Entry slippage is already included in entry_price.
            exit_value = investment / entry * cur * (1-engine.SLIPPAGE_RATE) * (1-engine.FEE_RATE)
            entry_cost = investment * (1+engine.FEE_RATE)
            pnl = exit_value - entry_cost
            roi = pnl / entry_cost * 100 if entry_cost else 0.0
            open_pnl += pnl
            open_invested += investment
            open_rows.append((pos.get("label", token[:10]), pnl, roi))

    cumulative = realized + open_pnl
    total_invested = invested_closed + open_invested
    roi = cumulative / total_invested * 100 if total_invested else 0.0

    stats = {
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "realized_pnl_sda": realized,
        "open_pnl_sda": open_pnl,
        "open_pnl_after_model_costs": True,
        "open_pnl_stale_quotes": stale_open_quotes,
        "open_pnl_basis": "last_observed_quote_not_verified_sell_fill",
        "cumulative_pnl_sda": cumulative,
        "total_invested_sda": total_invested,
        "roi_pct": roi,
        "closed_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / (wins + losses) * 100 if wins + losses else 0.0,
        "open_positions": len(positions),
    }
    from tools.paper_strategy_stats import cohort
    stats["tp4_paper"]=cohort(p)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    def pnl_line(label, value):
        icon = "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")
        return f"{icon} {label}: {value:+.2f} SDA"

    lines = [
        "📊 PAPER TRADING STATISTICS",
        "────────────────────────",
        pnl_line("CUMULATIVE P/L", cumulative),
        pnl_line("Realized P/L", realized),
        pnl_line("Open P/L", open_pnl),
        f"Open valuation after model costs • stale/unknown quotes: {stale_open_quotes}",
        f"📈 ROI: {roi:+.2f}%",
        f"💰 Invested: {total_invested:.2f} SDA",
        "",
        f"Closed trades: {len(closed)}",
        f"Wins / losses: {wins} / {losses}",
        f"Win rate: {stats['win_rate_pct']:.1f}%",
        f"Open positions: {len(positions)}",
    ]
    if open_rows:
        lines += ["", "OPEN P/L"]
        for label, pnl, pct in sorted(open_rows, key=lambda x: x[1]):
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            lines.append(f"{icon} {label}: {pnl:+.2f} SDA ({pct:+.2f}%)")
    lines += ["", "🎯 KPI = SDA accumulated, not USD profit."]
    diagnostic = [x for x in closed if x.get('pump_exit_observed_at')]
    if diagnostic:
        lines += ['', 'RECENT EXIT DIAGNOSTICS']
        for x in diagnostic[-5:][::-1]:
            gap = x.get('pump_exit_observation_gap_seconds')
            stop_gap = x.get('pump_exit_stop_gap_pct')
            lines.append(f"{x.get('label','?')}: ROI {num(x.get('closed_roi_pct')):+.2f}% | scan gap {gap if gap is not None else '?'}s | stop gap {stop_gap if stop_gap is not None else '?'}%")
    send("\n".join(lines))


if __name__ == "__main__":
    main()
