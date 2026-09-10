import copy
import re

import engine


def _pnl_icon(value):
    value = engine.num(value)
    return "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")


def _parse_wallet_value(lines, idx):
    for following in lines[idx + 1:idx + 4]:
        match = re.search(r"•\s*([0-9.,]+)\s+SDA(?:\s|$)", following)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def _token_names(wallet, meta):
    names = {}
    if not isinstance(wallet, dict) or not isinstance(meta, dict):
        return names
    for holding in wallet.get("holdings", []) or []:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").strip().upper()
        address = str(holding.get("address") or "").strip().lower()
        if not symbol:
            continue
        md = meta.get(address, {}) if address else {}
        name = holding.get("name") or (md.get("name") if isinstance(md, dict) else None)
        if name:
            names[symbol] = str(name).strip()
    return names


def _rebuild_summary_portfolio(portfolio, scanner, meta):
    if not isinstance(portfolio, dict):
        return {}
    rebuild = getattr(scanner, "_rebuild_fifo", None)
    if not callable(rebuild):
        return portfolio
    try:
        return rebuild(copy.deepcopy(portfolio), meta)
    except Exception as exc:
        print(f"Compact portfolio FIFO rebuild error: {exc}")
        return portfolio


def _sync_current_to_wallet(wallet, portfolio):
    """Make live wallet holdings authoritative for current/open P/L.

    portfolio_data can lag a real wallet sale. Scale the remaining FIFO cost
    to the wallet amount, then recompute P/L from the live wallet value. Do not
    scale an old P/L number because that double-counts the current market value.
    """
    result = copy.deepcopy(portfolio) if isinstance(portfolio, dict) else {}
    current = result.get("current", {})
    if not isinstance(current, dict) or not isinstance(wallet, dict):
        return result

    by_symbol = {}
    by_address = {}
    for holding in wallet.get("holdings", []) or []:
        if not isinstance(holding, dict):
            continue
        symbol = str(holding.get("symbol") or "").strip().upper()
        address = str(holding.get("address") or "").strip().lower()
        if symbol:
            by_symbol[symbol] = holding
        if address:
            by_address[address] = holding

    open_pnl = 0.0
    open_cost = 0.0
    for key, pf in current.items():
        if not isinstance(pf, dict):
            continue
        symbol = str(pf.get("symbol") or "").strip().upper()
        address = str(pf.get("address") or key).strip().lower()
        holding = by_address.get(address) or by_symbol.get(symbol)
        if not holding:
            continue
        wallet_amount = engine.num(holding.get("amount"))
        portfolio_amount = engine.num(pf.get("amount"))
        if portfolio_amount > 0 and wallet_amount < portfolio_amount:
            ratio = max(0.0, min(1.0, wallet_amount / portfolio_amount))
            pf["cost_sda"] = engine.num(pf.get("cost_sda")) * ratio
            pf["amount"] = wallet_amount
        elif portfolio_amount <= 0:
            pf["amount"] = wallet_amount

        cost = engine.num(pf.get("cost_sda"))
        value = engine.num(holding.get("value_sda"))
        if value <= 0:
            value = wallet_amount * engine.num(holding.get("price_sda"))
        pnl = value - cost
        pf["unrealized_pnl_sda"] = pnl
        pf["unrealized_pnl_pct"] = pnl / cost * 100 if cost > 0 else None
        open_cost += cost
        open_pnl += pnl

    result["open_pnl_sda"] = open_pnl
    result["open_unrealized_pnl_sda"] = open_pnl
    result["open_cost_sda"] = open_cost
    return result


def merge_wallet_portfolio(wallet, portfolio, scanner):
    meta = engine.load(engine.META_FILE, {})
    wallet_text = scanner.wallet_message_v16(wallet)
    lines = wallet_text.splitlines()

    names = _token_names(wallet, meta)
    for i, line in enumerate(lines):
        if not (line.startswith("🪙 ") and " — " in line):
            continue
        symbol = line[len("🪙 "):].split(" — ", 1)[0].strip().upper()
        current_name = line.split(" — ", 1)[1].strip()
        name = names.get(symbol)
        if name and current_name.upper() == symbol:
            lines[i] = f"🪙 {symbol} — {name}"

    portfolio = _sync_current_to_wallet(wallet, portfolio)
    current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
    by_key = {str(k).lower(): v for k, v in current.items() if isinstance(v, dict)}
    by_symbol = {}
    for key, value in by_key.items():
        symbol = str(value.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol[symbol] = value
    by_address = by_key

    out = []
    open_pnl = 0.0
    open_cost = 0.0
    known_open = 0

    for idx, line in enumerate(lines):
        if line.startswith("👛 REAL WALLET"):
            out.append("👛 REAL WALLET & PORTFOLIO")
            continue
        if any(marker in line for marker in ("Current open P/L", "Historical matched P/L", "Historical realized P/L", "Known total P/L", "Total P/L", "Matched sells:")):
            continue
        out.append(line)
        if not (line.startswith("🪙 ") and " — " in line):
            continue

        symbol = line[len("🪙 "):].split(" — ", 1)[0].strip()
        pf = by_symbol.get(symbol.upper()) or by_key.get(symbol.lower())
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
        out.append(f"   {_pnl_icon(pnl_n)} P/L {pnl_n:+.2f} SDA ({pct_n:+.2f}%)")
        if cost > 0:
            open_cost += cost
            open_pnl += pnl_n
            known_open += 1

    summary_portfolio = _rebuild_summary_portfolio(portfolio, scanner, meta)
    realized = summary_portfolio.get("realized_pnl_sda") if isinstance(summary_portfolio, dict) else None
    realized_known = realized is not None
    realized_n = engine.num(realized) if realized_known else 0.0
    engine_open = portfolio.get("open_pnl_sda") if isinstance(portfolio, dict) else None
    if engine_open is None and isinstance(portfolio, dict):
        engine_open = portfolio.get("open_unrealized_pnl_sda")
    open_n = engine.num(engine_open) if engine_open is not None else open_pnl
    total_known = open_n + realized_n if realized_known else None

    lines_summary = [
        "",
        "💹 P/L SUMMARY",
        f"{_pnl_icon(open_n)} Current open P/L {open_n:+.2f} SDA" + (f" ({open_n / open_cost * 100:+.2f}%)" if open_cost > 0 else ""),
    ]
    if realized_known:
        lines_summary.append(f"{_pnl_icon(realized_n)} Historical realized P/L {realized_n:+.2f} SDA")
        lines_summary.append(f"{_pnl_icon(total_known)} Total P/L {total_known:+.2f} SDA")
    else:
        lines_summary.append("⚪ Historical realized P/L UNKNOWN")
        lines_summary.append("⚪ Total P/L UNKNOWN")
    lines_summary.append(f"Matched positions: {known_open} • Cost basis: {open_cost:.2f} SDA")
    out.extend(lines_summary)
    return "\n".join(out)
