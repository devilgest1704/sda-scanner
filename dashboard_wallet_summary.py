"""Authoritative Real Wallet valuation/P&L summary for the main dashboard."""
from copy import deepcopy


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_wallet_summary_patched", False):
        return dashboard
    original_main_dashboard = dashboard.main_dashboard
    engine = dashboard.engine

    def _icon(v):
        return "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")

    def _wallet_maps(wallet):
        by_address = {}
        by_symbol = {}
        for holding in wallet.get("holdings", []) or []:
            if not isinstance(holding, dict):
                continue
            address = str(holding.get("address") or "").strip().lower()
            symbol = str(holding.get("symbol") or "").strip().upper()
            if address:
                by_address[address] = holding
            if symbol:
                by_symbol[symbol] = holding
        return by_address, by_symbol

    def _wallet_summary():
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = dashboard.load("portfolio_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = wallet if isinstance(wallet, dict) else {}
        portfolio = portfolio if isinstance(portfolio, dict) else {}
        meta = meta if isinstance(meta, dict) else {}

        # Rebuild the trade ledger first. This is the source of truth for FIFO
        # cost basis and realized P/L. Do NOT rescale an old cost basis to the
        # live wallet amount: that is wrong after a sell followed by a new buy.
        try:
            import main as scanner
            fifo = getattr(scanner, "_rebuild_fifo", None)
            if callable(fifo):
                portfolio = fifo(deepcopy(portfolio), meta)
        except Exception as exc:
            print(f"Wallet summary FIFO rebuild error: {exc}")

        known_token_value = engine.num(wallet.get("total_token_value_sda"))
        native_sda = engine.num(wallet.get("native_sda"))
        total_wallet_value = native_sda + known_token_value

        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        by_address, by_symbol = _wallet_maps(wallet)
        open_pnl = 0.0
        open_cost = 0.0
        matched_open = 0

        # FIFO 'current' already contains the correct remaining cost after all
        # sells and subsequent buys. Wallet is authoritative only for the live
        # market value. Never use wallet amount to scale FIFO cost basis.
        for key, position in current.items():
            if not isinstance(position, dict):
                continue
            address = str(position.get("address") or key).strip().lower()
            symbol = str(position.get("symbol") or "").strip().upper()
            holding = by_address.get(address) or by_symbol.get(symbol)
            if not holding:
                continue
            cost = engine.num(position.get("cost_sda"))
            value = engine.num(holding.get("value_sda"))
            if value <= 0:
                amount = engine.num(holding.get("amount"))
                price = engine.num(holding.get("price_sda"))
                value = amount * price if price > 0 else 0.0
            if cost <= 0 and value <= 0:
                continue
            pnl = value - cost
            open_cost += cost
            open_pnl += pnl
            matched_open += 1

        # _rebuild_fifo maintains the realized aggregate across ALL matched
        # sells, including partial sells. The previous implementation manually
        # counted only sells whose matched amount was >=99% of the sell amount,
        # so partial sells disappeared from Historical/Total P/L.
        realized_value = portfolio.get("realized_pnl_sda") if isinstance(portfolio, dict) else None
        realized_known = realized_value is not None
        realized = engine.num(realized_value) if realized_known else 0.0

        total_pnl = realized + open_pnl if realized_known else None
        open_pct = open_pnl / open_cost * 100 if open_cost else None

        lines = [
            "────────────────────────",
            f"📊 Known token value: {known_token_value:.2f} SDA",
            f"💼 TOTAL WALLET VALUE: {total_wallet_value:.2f} SDA",
            "Source: /addresses/.../token-balances",
            "",
            "💹 P/L SUMMARY",
            f"{_icon(open_pnl)} Current open P/L {open_pnl:+.2f} SDA" + (f" ({open_pct:+.2f}%)" if open_pct is not None else ""),
            f"{_icon(realized)} Historical realized P/L {realized:+.2f} SDA" if realized_known else "⚪ Historical realized P/L UNKNOWN",
            f"{_icon(total_pnl)} Total P/L {total_pnl:+.2f} SDA" if total_pnl is not None else "⚪ Total P/L UNKNOWN",
        ]
        return lines

    def wrapped(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        if not isinstance(text, str) or "💹 P/L SUMMARY" in text:
            return text
        summary = "\n".join(_wallet_summary())
        marker = "────────────────────────\n📂 DETAIL MENU"
        return text.replace(marker, summary + "\n" + marker, 1) if marker in text else text.rstrip() + "\n\n" + summary

    dashboard.main_dashboard = wrapped
    dashboard._sda_wallet_summary_patched = True

    try:
        import dashboard_v23_patch
        dashboard_v23_patch.patch_dashboard(dashboard)
    except Exception as exc:
        print(f"V23 dashboard patch error: {exc}")

    return dashboard
