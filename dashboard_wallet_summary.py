"""Authoritative Real Wallet valuation/P&L summary for the main dashboard."""
from copy import deepcopy


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_wallet_summary_patched", False):
        return dashboard
    original_main_dashboard = dashboard.main_dashboard
    engine = dashboard.engine

    def _icon(v):
        return "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")

    def _wallet_summary():
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = dashboard.load("portfolio_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = wallet if isinstance(wallet, dict) else {}
        portfolio = portfolio if isinstance(portfolio, dict) else {}
        meta = meta if isinstance(meta, dict) else {}

        # Never trust cached aggregate P/L fields. Rebuild FIFO from the trade
        # ledger, then sync the live open holdings to the wallet snapshot.
        try:
            import main as scanner
            fifo = getattr(scanner, "_rebuild_fifo", None)
            if callable(fifo):
                portfolio = fifo(deepcopy(portfolio), meta)
        except Exception as exc:
            print(f"Wallet summary FIFO rebuild error: {exc}")
        try:
            from telegram_dashboard_compact import _sync_current_to_wallet
            portfolio = _sync_current_to_wallet(wallet, portfolio)
        except Exception as exc:
            print(f"Wallet summary live sync error: {exc}")

        known_token_value = engine.num(wallet.get("total_token_value_sda"))
        native_sda = engine.num(wallet.get("native_sda"))
        total_wallet_value = native_sda + known_token_value

        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        open_pnl = sum(engine.num(x.get("unrealized_pnl_sda")) for x in current.values() if isinstance(x, dict))
        open_cost = sum(engine.num(x.get("cost_sda")) for x in current.values() if isinstance(x, dict) and x.get("unrealized_pnl_sda") is not None)

        realized = 0.0
        for tr in portfolio.get("trades", []) if isinstance(portfolio, dict) else []:
            if not isinstance(tr, dict) or str(tr.get("side") or "").upper() != "SELL":
                continue
            amount = engine.num(tr.get("amount")); matched = engine.num(tr.get("matched_amount"))
            if amount > 0 and matched >= amount * 0.99:
                realized += engine.num(tr.get("matched_proceeds_sda")) - engine.num(tr.get("cost_basis_sda"))
        total_pnl = realized + open_pnl
        open_pct = open_pnl / open_cost * 100 if open_cost else None

        lines = [
            "────────────────────────",
            f"📊 Known token value: {known_token_value:.2f} SDA",
            f"💼 TOTAL WALLET VALUE: {total_wallet_value:.2f} SDA",
            "Source: /addresses/.../token-balances",
            "",
            "💹 P/L SUMMARY",
            f"{_icon(open_pnl)} Current open P/L {open_pnl:+.2f} SDA" + (f" ({open_pct:+.2f}%)" if open_pct is not None else ""),
            f"{_icon(realized)} Historical realized P/L {realized:+.2f} SDA",
            f"{_icon(total_pnl)} Total P/L {total_pnl:+.2f} SDA",
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
    return dashboard
