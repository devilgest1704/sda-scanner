import re

import engine


def _pnl_icon(value):
    value = engine.num(value)
    return "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")


def _parse_wallet_value(lines, idx):
    """Read the SDA value from the wallet holding line following the token name."""
    for following in lines[idx + 1:idx + 4]:
        match = re.search(r"•\s*([0-9.,]+)\s+SDA(?:\s|$)", following)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def merge_wallet_portfolio(wallet, portfolio, scanner):
    """Render one compact wallet/portfolio view with per-token and total P/L."""
    wallet_text = scanner.wallet_message_v16(wallet)
    lines = wallet_text.splitlines()

    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    by_key = {str(k).lower(): v for k, v in current.items() if isinstance(v, dict)}
    by_symbol = {}
    for key, value in by_key.items():
        symbol = str(value.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol[symbol] = value

    # Also index by on-chain address. wallet_data.json contains the address
    # explicitly, so this is the most reliable fallback when symbols differ
    # only by case/formatting.
    by_address = by_key

    out = []
    open_pnl = 0.0
    open_cost = 0.0
    known_open = 0

    for idx, line in enumerate(lines):
        if line.startswith("👛 REAL WALLET"):
            out.append("👛 REAL WALLET & PORTFOLIO")
            continue

        # The canonical wallet renderer may already contain its own P/L block.
        # Remove it and append one deterministic block after all holdings.
        if any(marker in line for marker in (
            "Current open P/L",
            "Historical matched P/L",
            "Historical realized P/L",
            "Known total P/L",
            "Matched sells:",
        )):
            continue

        out.append(line)

        if not (line.startswith("🪙 ") and " — " in line):
            continue

        # IMPORTANT: do not use line[3:] here. The 🪙 emoji + space occupy
        # only two code points, so line[3:] would drop the first symbol letter
        # (DMCS -> MCS, FBAY -> BAY, etc.) and break portfolio matching.
        symbol = line[len("🪙 "):].split(" — ", 1)[0].strip()
        pf = by_symbol.get(symbol.upper()) or by_key.get(symbol.lower())

        # If the wallet line has an address, prefer exact address matching.
        # wallet_message_v16 normally has the address available in wallet data,
        # but keep symbol matching as the normal path for compatibility.
        if pf is None:
            for holding in (wallet.get("holdings", []) if isinstance(wallet, dict) else []):
                if not isinstance(holding, dict):
                    continue
                if str(holding.get("symbol") or "").strip().upper() == symbol.upper():
                    address = str(holding.get("address") or "").lower()
                    pf = by_address.get(address)
                    if pf is not None:
                        break

        if pf is None:
            out.append("   ⚪ P/L N/A • no matched portfolio position")
            continue

        pnl = pf.get("unrealized_pnl_sda")
        pct = pf.get("unrealized_pnl_pct")
        cost = engine.num(pf.get("cost_sda"))

        # Calculate from the displayed wallet value + FIFO cost basis if the
        # persisted derived P/L fields are temporarily missing.
        if pnl is None and cost > 0:
            value_sda = engine.num(pf.get("value_sda"))
            if value_sda <= 0:
                value_sda = _parse_wallet_value(lines, idx) or 0.0
            if value_sda > 0:
                pnl = value_sda - cost
                pct = pnl / cost * 100

        if pnl is None:
            out.append("   ⚪ P/L N/A • no valid cost basis")
            continue

        pnl_n = engine.num(pnl)
        pct_n = engine.num(pct)
        icon = _pnl_icon(pnl_n)
        out.append(f"   {icon} P/L {pnl_n:+.2f} SDA ({pct_n:+.2f}%)")

        if cost > 0:
            open_cost += cost
            open_pnl += pnl_n
            known_open += 1

    realized = portfolio.get("realized_pnl_sda") if isinstance(portfolio, dict) else None
    realized_known = realized is not None
    realized_n = engine.num(realized) if realized_known else 0.0

    # Prefer the engine aggregate when available; otherwise use the same
    # per-position P/L values that are displayed above.
    engine_open = portfolio.get("open_pnl_sda") if isinstance(portfolio, dict) else None
    open_n = engine.num(engine_open) if engine_open is not None else open_pnl

    total_known = open_n + realized_n if realized_known else None
    lines_summary = [
        "",
        "💹 P/L SUMMARY",
        f"{_pnl_icon(open_n)} Current open P/L {open_n:+.2f} SDA" + (f" ({open_n / open_cost * 100:+.2f}%)" if open_cost > 0 else ""),
    ]
    if realized_known:
        lines_summary.append(f"{_pnl_icon(realized_n)} Historical realized P/L {realized_n:+.2f} SDA")
        lines_summary.append(f"{_pnl_icon(total_known)} Known total P/L {total_known:+.2f} SDA")
    else:
        lines_summary.append("⚪ Historical realized P/L UNKNOWN")
        lines_summary.append("⚪ Known total P/L UNKNOWN")
    lines_summary.append(f"Matched positions: {known_open} • Cost basis: {open_cost:.2f} SDA")

    out.extend(lines_summary)
    return "\n".join(out)
