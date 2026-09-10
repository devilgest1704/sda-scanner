import re

import engine


def merge_wallet_portfolio(wallet, portfolio, scanner):
    """Render the canonical wallet once and always show P/L when cost basis exists."""
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
    for idx, line in enumerate(lines):
        if line.startswith("👛 REAL WALLET"):
            out.append("👛 REAL WALLET & PORTFOLIO")
            continue

        out.append(line)

        if not (line.startswith("🪙 ") and " — " in line):
            continue

        symbol = line[3:].split(" — ", 1)[0].strip()
        pf = by_symbol.get(symbol.upper()) or by_key.get(symbol.lower())
        if pf is None:
            continue

        pnl = pf.get("unrealized_pnl_sda")
        pct = pf.get("unrealized_pnl_pct")

        # Calculate from the displayed wallet value + FIFO cost basis if the
        # persisted derived P/L fields are temporarily missing.
        if pnl is None:
            cost = engine.num(pf.get("cost_sda"))
            if cost > 0:
                for following in lines[idx + 1:idx + 4]:
                    match = re.search(r"•\s*([0-9.,]+)\s+SDA(?:\s|$)", following)
                    if match:
                        value_sda = float(match.group(1).replace(",", ""))
                        pnl = value_sda - cost
                        pct = pnl / cost * 100
                        break

        if pnl is None:
            out.append("   ⚪ P/L N/A • no cost basis")
            continue

        pnl_n = engine.num(pnl)
        pct_n = engine.num(pct)
        icon = "🟢" if pnl_n > 0 else ("🔴" if pnl_n < 0 else "⚪")
        out.append(f"   {icon} P/L {pnl_n:+.2f} SDA ({pct_n:+.2f}%)")

    return "\n".join(out)
