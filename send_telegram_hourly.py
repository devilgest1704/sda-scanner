import telegram_dashboard as dashboard
import telegram_dashboard_compact as compact
import main as scanner


def main():
    dashboard._merge_wallet_portfolio = lambda wallet, portfolio, md, ws, meta: compact.merge_wallet_portfolio(
        wallet, portfolio, scanner
    )
    dashboard.run()


if __name__ == "__main__":
    main()
