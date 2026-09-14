"""Final read-only dashboard consistency layer.

Keeps the Telegram dashboard, Real Wallet view and position actions aligned
with the live wallet balances while preserving FIFO cost basis from the trade
ledger. No trading/execution logic is changed.
"""
from copy import deepcopy


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_data_sync_patched", False):
        return dashboard

    engine = dashboard.engine
    original_merge = getattr(dashboard, "_merge_wallet_portfolio", None)
    original_position = getattr(dashboard, "_position_recommendations", None)
    original_real = getattr(dashboard, "real_trading_report", None)
    original_main = getattr(dashboard, "main_dashboard", None)

    def _rebuild(portfolio, meta):
        result = deepcopy(portfolio) if isinstance(portfolio, dict) else {}
        try:
            import main as scanner
            fifo = getattr(scanner, "_rebuild_fifo", None)
            if callable(fifo):
                result = fifo(result, meta if isinstance(meta, dict) else {})
        except Exception as exc:
            print(f"Dashboard sync FIFO rebuild error: {exc}")
        return result

    def _sync(wallet, portfolio):
        wallet = wallet if isinstance(wallet, dict) else {}
        meta = dashboard.load("token_metadata.json", {})
        result = _rebuild(portfolio, meta)
        current = result.get("current", {})
        if not isinstance(current, dict):
            current = {}

        by_address = {}
        by_symbol = {}
        for h in wallet.get("holdings", []) or []:
            if not isinstance(h, dict):
                continue
            address = str(h.get("address") or "").strip().lower()
            symbol = str(h.get("symbol") or "").strip().upper()
            if address:
                by_address[address] = h
            if symbol:
                by_symbol.setdefault(symbol, []).append(h)

        synced = {}
        for key, raw in current.items():
            if not isinstance(raw, dict):
                continue
            p = deepcopy(raw)
            address = str(p.get("address") or p.get("token_address") or key).strip().lower()
            symbol = str(p.get("symbol") or "").strip().upper()
            holding = by_address.get(address)
            if holding is None and symbol and len(by_symbol.get(symbol, [])) == 1:
                holding = by_symbol[symbol][0]
            if holding is None:
                continue

            cost = engine.num(p.get("cost_sda"))
            live_value = engine.num(holding.get("value_sda"))
            amount = engine.num(holding.get("amount"))
            price = engine.num(holding.get("price_sda"))
            if live_value <= 0 and amount > 0 and price > 0:
                live_value = amount * price

            p["address"] = holding.get("address") or p.get("address") or key
            p["symbol"] = holding.get("symbol") or p.get("symbol")
            p["wallet_amount"] = amount
            p["wallet_price_sda"] = price
            p["wallet_value_sda"] = live_value
            p["current_price"] = price
            p["current_value_sda"] = live_value
            if cost > 0:
                pnl = live_value - cost
                p["unrealized_pnl_sda"] = pnl
                p["unrealized_pnl_pct"] = pnl / cost * 100.0
            else:
                p["unrealized_pnl_sda"] = None
                p["unrealized_pnl_pct"] = None
            synced[key] = p

        result["current"] = synced
        result["open_cost_sda"] = sum(engine.num(p.get("cost_sda")) for p in synced.values())
        result["open_pnl_sda"] = sum(engine.num(p.get("unrealized_pnl_sda")) for p in synced.values())
        result["open_unrealized_pnl_sda"] = result["open_pnl_sda"]
        result["wallet_holding_count"] = len(wallet.get("holdings", []) or [])
        result["matched_position_count"] = len(synced)
        result["wallet_updated_at"] = wallet.get("updated_at")
        return result

    def merge_sync(wallet, portfolio, md, ws, meta):
        synced = _sync(wallet, portfolio)
        if callable(original_merge):
            return original_merge(wallet, synced, md, ws, meta)
        return synced

    def positions_sync(md, ws, meta, portfolio):
        wallet = dashboard.load("wallet_data.json", {})
        synced = _sync(wallet, portfolio)
        if callable(original_position):
            return original_position(md, ws, meta, synced)
        return []

    def real_sync(*args, **kwargs):
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = _sync(wallet, dashboard.load("portfolio_data.json", {}))
        view = dashboard._merge_wallet_portfolio(wallet, portfolio, md, ws, meta)
        rows = dashboard._position_recommendations(md, ws, meta, portfolio)
        lines = [view, "", "🧭 POSITION ACTION", "────────────────────────"]
        if not rows:
            lines.append("⚪ No tracked wallet positions with FIFO cost basis")
        else:
            for r in rows:
                pnl = "UNKNOWN" if r.get("pnl_sda") is None else f"{engine.num(r.get('pnl_sda')):+.2f} SDA"
                score = "N/A" if r.get("score") is None else f"{engine.num(r.get('score')):.0f}/100"
                icon = dashboard._sda_action_icon(r.get("action")) if hasattr(dashboard, "_sda_action_icon") else "🟡"
                lines += [f"{icon} {r.get('symbol')}: {r.get('action')} • P/L {pnl} • score {score}", f"   {r.get('reason')}"]
        wallet_count = len(wallet.get("holdings", []) or []) if isinstance(wallet, dict) else 0
        matched = len(portfolio.get("current", {}) or {}) if isinstance(portfolio, dict) else 0
        lines += ["", f"🔗 DATA SYNC • wallet holdings {wallet_count} • tracked/FIFO matched {matched}", "────────────────────────", "👁 READ-ONLY • No real order is executed"]
        return "\n".join(lines)

    def main_sync(*args, **kwargs):
        if not callable(original_main):
            return ""
        text = original_main(*args, **kwargs)
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = _sync(wallet, dashboard.load("portfolio_data.json", {}))
        wallet_count = len(wallet.get("holdings", []) or []) if isinstance(wallet, dict) else 0
        matched = len(portfolio.get("current", {}) or {}) if isinstance(portfolio, dict) else 0
        marker = "👛 REAL WALLET"
        if marker in text and "🔗 DATA SYNC" not in text:
            text = text.replace(marker, marker + f"\n🔗 DATA SYNC • wallet holdings {wallet_count} • tracked/FIFO matched {matched}", 1)
        return text

    dashboard._wallet_synced_portfolio = _sync
    if callable(original_merge):
        dashboard._merge_wallet_portfolio = merge_sync
    if callable(original_position):
        dashboard._position_recommendations = positions_sync
    # Older dashboard builds do not expose real_trading_report. Do not treat
    # that as an error: the sync layer only wraps it when it actually exists.
    if callable(original_real):
        dashboard.real_trading_report = real_sync
    if callable(original_main):
        dashboard.main_dashboard = main_sync
    dashboard._sda_data_sync_patched = True
    return dashboard
