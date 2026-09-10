import engine


def merge_wallet_portfolio(wallet, portfolio, scanner):
    """Render the canonical wallet once and merge portfolio P/L into matching holdings."""
    wallet_text = scanner.wallet_message_v16(wallet)
    lines = wallet_text.splitlines()
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    by_key = {str(k).lower(): v for k, v in current.items() if isinstance(v, dict)}
    by_symbol = {}
    for key, value in by_key.items():
        symbol = str(value.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol[symbol] = value

    out = []
    for line in lines:
        if line.startswith("👛 REAL WALLET"):
            out.append("👛 REAL WALLET & PORTFOLIO")
            continue
        out.append(line)
        if line.startswith("🪙 ") and " — " in line:
            symbol = line[3:].split(" — ", 1)[0].strip()
            pf = by_symbol.get(symbol.upper())
            if pf is None:
                pf = by_key.get(symbol.lower())
            if pf is not None:
                pnl = pf.get("unrealized_pnl_sda")
                pct = pf.get("unrealized_pnl_pct")
                if pnl is not None:
                    pnl_n = engine.num(pnl)
                    pct_n = engine.num(pct)
                    icon = "🟢" if pnl_n > 0 else ("🔴" if pnl_n < 0 else "⚪")
                    out.append(f"   {icon} P/L {pnl_n:+.2f} SDA ({pct_n:+.2f}%)")
    return "\n".join(out)
