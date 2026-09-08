import json, os, requests

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
    for address, pos in positions.items():
        token = str(address).lower()
        td = tokens.get(token, {}) if isinstance(tokens, dict) else {}
        an = td.get("analysis", td) if isinstance(td, dict) else {}
        cur = num(an.get("price_in_sda"))
        entry = num(pos.get("entry_price"))
        fraction = num(pos.get("remaining_fraction", 1))
        investment = num(pos.get("investment_sda")) * fraction
        if cur > 0 and entry > 0:
            roi = (cur - entry) / entry * 100
            pnl = investment * roi / 100
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
        "cumulative_pnl_sda": cumulative,
        "total_invested_sda": total_invested,
        "roi_pct": roi,
        "closed_trades": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / (wins + losses) * 100 if wins + losses else 0.0,
        "open_positions": len(positions),
    }
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
    send("\n".join(lines))


if __name__ == "__main__":
    main()
