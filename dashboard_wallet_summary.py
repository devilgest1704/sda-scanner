"""Restore the Real Wallet valuation/P&L summary on the main dashboard."""


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_wallet_summary_patched", False):
        return dashboard

    original_main_dashboard = dashboard.main_dashboard
    engine = dashboard.engine

    def _pnl_icon(value):
        if value > 0:
            return "🟢"
        if value < 0:
            return "🔴"
        return "⚪"

    def _wallet_summary():
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = dashboard.load("portfolio_data.json", {})
        if not isinstance(wallet, dict):
            wallet = {}
        if not isinstance(portfolio, dict):
            portfolio = {}

        known_token_value = engine.num(wallet.get("total_token_value_sda"))
        native_sda = engine.num(wallet.get("native_sda"))
        total_wallet_value = engine.num(wallet.get("total_value_sda"))
        if total_wallet_value == 0 and (known_token_value or native_sda):
            total_wallet_value = native_sda + known_token_value

        open_pnl = portfolio.get("open_unrealized_pnl_sda")
        if open_pnl is None:
            open_pnl = portfolio.get("open_pnl_sda")
        if open_pnl is None:
            current = portfolio.get("current", {})
            open_pnl = sum(
                engine.num(x.get("unrealized_pnl_sda"))
                for x in current.values()
                if isinstance(x, dict)
            )
        open_pnl = engine.num(open_pnl)

        realized = portfolio.get("realized_pnl_sda")
        if realized is None:
            realized = portfolio.get("historical_realized_pnl_sda")
        realized = engine.num(realized)

        total_pnl = portfolio.get("known_total_pnl_sda")
        if total_pnl is None:
            total_pnl = realized + open_pnl
        total_pnl = engine.num(total_pnl)

        open_cost = engine.num(portfolio.get("open_cost_sda"))
        open_pct = (open_pnl / open_cost * 100.0) if open_cost else None

        lines = [
            "────────────────────────",
            f"📊 Known token value: {known_token_value:.2f} SDA",
            f"💼 TOTAL WALLET VALUE: {total_wallet_value:.2f} SDA",
            "Source: /addresses/.../token-balances",
            "",
            "💹 P/L SUMMARY",
            f"{_pnl_icon(open_pnl)} Current open P/L {open_pnl:+.2f} SDA" + (f" ({open_pct:+.2f}%)" if open_pct is not None else ""),
            f"{_pnl_icon(realized)} Historical realized P/L {realized:+.2f} SDA",
            f"{_pnl_icon(total_pnl)} Total P/L {total_pnl:+.2f} SDA",
        ]
        return lines

    def _main_dashboard_with_wallet_summary(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        if not isinstance(text, str):
            return text
        # Never duplicate the block if another dashboard layer already added it.
        if "💹 P/L SUMMARY" in text:
            return text
        marker = "────────────────────────\n📂 DETAIL MENU"
        summary = "\n".join(_wallet_summary())
        if marker in text:
            return text.replace(marker, summary + "\n" + marker, 1)
        return text.rstrip() + "\n\n" + summary

    dashboard.main_dashboard = _main_dashboard_with_wallet_summary
    dashboard._sda_wallet_summary_patched = True
    return dashboard
