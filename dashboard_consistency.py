"""Keep Real Wallet and dashboard Position Action on one canonical data view."""
import re


def patch_dashboard(dashboard):
    engine = dashboard.engine
    original_recommendations = dashboard._position_recommendations
    original_merge_wallet = dashboard._merge_wallet_portfolio

    def _wallet_synced_portfolio(wallet, portfolio):
        result = dict(portfolio) if isinstance(portfolio, dict) else {}
        current = result.get("current", {})
        if not isinstance(current, dict) or not isinstance(wallet, dict):
            return result

        allowed_symbols = {
            str(h.get("symbol") or "").strip().upper()
            for h in wallet.get("holdings", []) or []
            if isinstance(h, dict)
        }
        allowed_addresses = {
            str(h.get("address") or "").strip().lower()
            for h in wallet.get("holdings", []) or []
            if isinstance(h, dict)
        }
        result["current"] = {
            key: value
            for key, value in current.items()
            if isinstance(value, dict)
            and (
                str(value.get("symbol") or "").strip().upper() in allowed_symbols
                or str(value.get("address") or key).strip().lower() in allowed_addresses
            )
        }
        result["open_cost_sda"] = sum(engine.num(x.get("cost_sda")) for x in result["current"].values())
        result["open_pnl_sda"] = sum(engine.num(x.get("unrealized_pnl_sda")) for x in result["current"].values())
        result["open_unrealized_pnl_sda"] = result["open_pnl_sda"]
        return result

    def _canonical_recommendations(md, ws, meta, portfolio):
        wallet = dashboard.load("wallet_data.json", {})
        synced = _wallet_synced_portfolio(wallet, portfolio)
        rows = original_recommendations(md, ws, meta, synced)
        if not isinstance(rows, list):
            return []
        # One ordering everywhere: best current SDA P/L first.
        return sorted(
            rows,
            key=lambda x: (
                x.get("pnl_sda") is None,
                -engine.num(x.get("pnl_sda")),
                str(x.get("symbol") or "").upper(),
            ),
        )

    def _sort_wallet_view(wallet, portfolio, md, ws, meta):
        text = original_merge_wallet(wallet, portfolio, md, ws, meta)
        if not text:
            return text
        lines = text.splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("🪙 ")]
        if len(starts) < 2:
            return text

        prefix = lines[:starts[0]]
        blocks = []
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            block = lines[start:end]
            pnl = None
            for line in block:
                match = re.search(r"P/L\s+([+-]?\d+(?:[.,]\d+)?)\s+SDA", line)
                if match:
                    try:
                        pnl = float(match.group(1).replace(",", ""))
                    except ValueError:
                        pnl = None
                    break
            blocks.append((pnl, block))

        blocks.sort(key=lambda item: (item[0] is None, -(item[0] or 0.0), item[1][0]))
        sorted_lines = list(prefix)
        for _, block in blocks:
            sorted_lines.extend(block)
        return "\n".join(sorted_lines)

    def real_trading_report_canonical():
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = dashboard.load("portfolio_data.json", {})
        portfolio = _wallet_synced_portfolio(wallet, portfolio)
        wallet_view = dashboard._merge_wallet_portfolio(wallet, portfolio, md, ws, meta)
        rows = _canonical_recommendations(md, ws, meta, portfolio)

        lines = [wallet_view, "", "🧭 POSITION ACTION", "────────────────────────"]
        if not rows:
            lines.append("⚪ No actionable real positions")
        else:
            for row in rows:
                action = row.get("action")
                icon = (
                    "🚨" if action == "EMERGENCY SELL" else
                    "🔴" if action == "SELL / EXIT" else
                    "🟠" if action == "PARTIAL SELL" else
                    "🟢" if action == "HOLD / TRAIL" else "🟡"
                )
                pnl = "UNKNOWN" if row.get("pnl_sda") is None else f"{engine.num(row.get('pnl_sda')):+.2f} SDA"
                score = f"{engine.num(row.get('score')):.0f}/100"
                lines.append(f"{icon} {row.get('symbol')}: {action} • P/L {pnl} • score {score}")
                lines.append(f"   {row.get('reason')}")
        lines += ["", "────────────────────────", "👁 READ-ONLY • No real order is executed"]
        return "\n".join(lines)

    dashboard._position_recommendations = _canonical_recommendations
    dashboard._merge_wallet_portfolio = _sort_wallet_view
    dashboard.real_trading_report = real_trading_report_canonical
    dashboard._sda_dashboard_consistency_patched = True
    return dashboard
