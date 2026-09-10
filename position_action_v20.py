"""Shared V20/V21 position-action policy for Telegram display."""
from copy import deepcopy

from strategy_v21 import patch_engine, technical_sell_confirmed, evaluate_exit


def _pnl_value(value, unit):
    value = float(value or 0)
    icon = "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")
    return f"{icon} {value:+.2f} {unit}"


def patch_dashboard(dashboard):
    original_main_dashboard = dashboard.main_dashboard
    original_handle_update = dashboard.handle_update
    original_menu_keyboard = dashboard.menu_keyboard

    if not getattr(dashboard.engine, "_sda_v21_patched", False):
        patch_engine(dashboard.engine)
        dashboard.engine._sda_v21_patched = True

    def _resolve_token_data(tokens, token, pf, meta):
        candidates = []
        for value in (token, pf.get("address") if isinstance(pf, dict) else None, pf.get("token") if isinstance(pf, dict) else None):
            if value:
                candidates.append(str(value).strip().lower())
        symbol = str((pf or {}).get("symbol") or "").strip().upper() if isinstance(pf, dict) else ""
        for key in candidates:
            if key in tokens:
                return key, dashboard._analysis(tokens.get(key, {}) or {})
        for key, value in tokens.items():
            key_norm = str(key).strip().lower()
            if key_norm in candidates:
                return key, dashboard._analysis(value or {})
            if isinstance(value, dict):
                address = str(value.get("address") or "").strip().lower()
                if address and address in candidates:
                    return key, dashboard._analysis(value)
                analysis = value.get("analysis")
                if isinstance(analysis, dict):
                    address = str(analysis.get("address") or "").strip().lower()
                    if address and address in candidates:
                        return key, dashboard._analysis(value)
        if symbol:
            for key, value in tokens.items():
                if not isinstance(value, dict):
                    continue
                analysis = dashboard._analysis(value)
                sym = str(value.get("symbol") or analysis.get("symbol") or "").strip().upper()
                if sym == symbol:
                    return key, analysis
            for key, value in (meta.items() if isinstance(meta, dict) else []):
                if not isinstance(value, dict) or str(value.get("symbol") or "").strip().upper() != symbol:
                    continue
                address = str(value.get("address") or key).strip().lower()
                if address in tokens:
                    return address, dashboard._analysis(tokens.get(address, {}) or {})
                for market_key, market_value in tokens.items():
                    if str(market_key).strip().lower() == address:
                        return market_key, dashboard._analysis(market_value or {})
        return None, {}

    def _wallet_synced_portfolio(wallet, portfolio):
        """Use the live wallet amount as authoritative for current/open P/L."""
        result = deepcopy(portfolio) if isinstance(portfolio, dict) else {}
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
            amount = dashboard.engine.num(holding.get("amount"))
            if symbol:
                by_symbol[symbol] = amount
            if address:
                by_address[address] = amount

        total_open_pnl = 0.0
        total_open_cost = 0.0
        for key, pf in current.items():
            if not isinstance(pf, dict):
                continue
            symbol = str(pf.get("symbol") or "").strip().upper()
            address = str(pf.get("address") or key).strip().lower()
            wallet_amount = by_address.get(address)
            if wallet_amount is None and symbol:
                wallet_amount = by_symbol.get(symbol)
            portfolio_amount = dashboard.engine.num(pf.get("amount"))

            # If the wallet already contains less than portfolio_data.json, the
            # ledger is lagging. Scale only the stale OPEN position to the actual
            # remaining wallet amount so sold tokens are not shown as open P/L.
            if wallet_amount is not None and portfolio_amount > 0 and wallet_amount < portfolio_amount:
                ratio = max(0.0, min(1.0, wallet_amount / portfolio_amount))
                if pf.get("cost_sda") is not None:
                    pf["cost_sda"] = dashboard.engine.num(pf.get("cost_sda")) * ratio
                if pf.get("unrealized_pnl_sda") is not None:
                    pf["unrealized_pnl_sda"] = dashboard.engine.num(pf.get("unrealized_pnl_sda")) * ratio
                pf["amount"] = wallet_amount
                cost = dashboard.engine.num(pf.get("cost_sda"))
                pnl = dashboard.engine.num(pf.get("unrealized_pnl_sda"))
                if cost > 0:
                    pf["unrealized_pnl_pct"] = pnl / cost * 100

            total_open_cost += dashboard.engine.num(pf.get("cost_sda"))
            total_open_pnl += dashboard.engine.num(pf.get("unrealized_pnl_sda"))

        result["open_pnl_sda"] = total_open_pnl
        result["open_unrealized_pnl_sda"] = total_open_pnl
        result["open_cost_sda"] = total_open_cost
        return result

    def _sync_current_to_wallet(wallet, portfolio):
        result = _wallet_synced_portfolio(wallet, portfolio)
        current = result.get("current", {})
        if not isinstance(current, dict):
            return result
        allowed_symbols = {str(h.get("symbol") or "").strip().upper() for h in (wallet.get("holdings", []) or []) if isinstance(h, dict)}
        allowed_addresses = {str(h.get("address") or "").strip().lower() for h in (wallet.get("holdings", []) or []) if isinstance(h, dict)}
        result["current"] = {
            key: pf for key, pf in current.items()
            if isinstance(pf, dict) and (
                str(pf.get("symbol") or "").strip().upper() in allowed_symbols
                or str(pf.get("address") or key).strip().lower() in allowed_addresses
            )
        }
        return result

    def position_recommendations_v21(md, ws, meta, portfolio):
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        auto_state = dashboard.load("paper_auto_state.json", {})
        rows = []
        for token, pf in current.items():
            if not isinstance(pf, dict):
                continue
            pnl_raw = pf.get("unrealized_pnl_pct")
            cost = pf.get("cost_sda")
            if cost is None or pnl_raw is None:
                continue
            market_key, analysis = _resolve_token_data(tokens, token, pf, meta)
            try:
                signal = dashboard.engine.score(market_key or token, analysis, ws) if analysis else {"confidence": 0, "m1h": 0, "net_1h": 0}
            except Exception:
                signal = {"confidence": 0, "m1h": 0, "net_1h": 0}
            score = dashboard.engine.num(signal.get("confidence"))
            m1h = dashboard.engine.num(signal.get("m1h"))
            flow1 = dashboard.engine.num(signal.get("net_1h"))
            pnl = dashboard.engine.num(pnl_raw)
            bearish = technical_sell_confirmed(analysis) if analysis else False
            exit_signal = evaluate_exit(pnl, score, m1h, flow1, bearish)
            old = auto_state.get(token, {}) if isinstance(auto_state, dict) else {}
            if not old and market_key:
                old = auto_state.get(market_key, {}) if isinstance(auto_state, dict) else {}
            neg = int(dashboard.engine.num(old.get("neg")))
            weak = int(dashboard.engine.num(old.get("weak")))
            tp1_hit = bool(pf.get("tp1_hit"))
            neg = min(5, neg + 1) if exit_signal["negative"] else 0
            weak = min(5, weak + 1) if exit_signal["weakening"] else 0
            if exit_signal["emergency"]:
                action = "EMERGENCY SELL"; reason = "V21 emergency: ROI ≤ -15%, score <35, negative momentum + flow"
            elif weak >= 2 and not tp1_hit:
                action = "PARTIAL SELL"; reason = "V21 weakening confirmed 2 times"
            elif neg >= 3:
                action = "SELL / EXIT"; reason = "V21 negative exit confirmed 3 times"
            elif exit_signal["weakening"]:
                action = "HOLD / WATCH"; reason = f"weakening signal {weak}/2 confirmations"
            elif exit_signal["negative"]:
                action = "HOLD / WATCH"; reason = f"negative exit signal {neg}/3 confirmations"
            elif score >= 70 and m1h > 0 and flow1 > 0:
                action = "HOLD / TRAIL"; reason = "positive trend and SDA flow"
            else:
                action = "HOLD / WATCH"; reason = "no confirmed exit condition"
            rows.append({"token": token, "symbol": pf.get("symbol") or dashboard.engine.lbl(token, meta), "action": action, "reason": reason, "pnl_pct": pnl_raw, "pnl_sda": pf.get("unrealized_pnl_sda"), "score": score, "m1h": m1h, "flow_1h": flow1})
        order = {"EMERGENCY SELL": 0, "SELL / EXIT": 1, "PARTIAL SELL": 2, "HOLD / TRAIL": 3, "HOLD / WATCH": 4}
        return sorted(rows, key=lambda x: (order.get(x.get("action"), 9), -dashboard.engine.num(x.get("score"))))

    def real_trading_report():
        md = dashboard.load("market_data.json", {"tokens": {}})
        ws = dashboard.load("whale_data.json", {})
        meta = dashboard.load("token_metadata.json", {})
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = _sync_current_to_wallet(wallet, dashboard.load("portfolio_data.json", {}))
        wallet_view = dashboard._merge_wallet_portfolio(wallet, portfolio, md, ws, meta)
        rows = position_recommendations_v21(md, ws, meta, portfolio)
        lines = [wallet_view, "", "🧭 POSITION ACTION", "────────────────────────"]
        if not rows:
            lines.append("⚪ No actionable real positions")
        else:
            for row in rows:
                action = row["action"]
                icon = "🚨" if action == "EMERGENCY SELL" else "🔴" if action == "SELL / EXIT" else "🟠" if action == "PARTIAL SELL" else "🟡"
                pnl = "UNKNOWN" if row["pnl_sda"] is None else f"{dashboard.engine.num(row['pnl_sda']):+.2f} SDA"
                score = "N/A" if row["score"] is None else f"{row['score']:.0f}/100"
                lines.append(f"{icon} {row['symbol']}: {action} • P/L {pnl} • score {score}")
                lines.append(f"   {row['reason']}")
        lines += ["", "────────────────────────", "👁 READ-ONLY • No real order is executed"]
        return "\n".join(lines)

    def real_statistics_report():
        wallet = dashboard.load("wallet_data.json", {})
        portfolio = _sync_current_to_wallet(wallet, dashboard.load("portfolio_data.json", {}))
        meta = dashboard.load("token_metadata.json", {})
        import main as scanner
        fifo = getattr(scanner, "_rebuild_fifo", None)
        if callable(fifo):
            try:
                portfolio = fifo(deepcopy(portfolio), meta)
            except Exception as exc:
                print(f"Real statistics FIFO rebuild error: {exc}")
        trades = portfolio.get("trades", []) if isinstance(portfolio, dict) else []
        current = portfolio.get("current", {}) if isinstance(portfolio, dict) else {}
        trades = trades if isinstance(trades, list) else []
        current = current if isinstance(current, dict) else {}
        sells = [x for x in trades if isinstance(x, dict) and str(x.get("side") or "").upper() == "SELL" and dashboard.engine.num(x.get("matched_amount")) > 0 and dashboard.engine.num(x.get("cost_basis_sda")) >= 0]
        buys = [x for x in trades if isinstance(x, dict) and str(x.get("side") or "").upper() == "BUY"]
        profits = [dashboard.engine.num(x.get("matched_proceeds_sda")) - dashboard.engine.num(x.get("cost_basis_sda")) for x in sells]
        realized = dashboard.engine.num(portfolio.get("realized_pnl_sda"))
        wins = [x for x in profits if x > 0]; losses = [x for x in profits if x < 0]
        open_pnl = dashboard.engine.num(portfolio.get("open_pnl_sda"))
        open_cost = dashboard.engine.num(portfolio.get("open_cost_sda"))
        total = realized + open_pnl
        win_rate = len(wins) / len(profits) * 100 if profits else 0.0
        profit_factor = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        best = max(profits) if profits else 0.0
        worst = min(profits) if profits else 0.0
        fmt_pf = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        lines = ["📈 REAL TRADING • STATISTICS", "", f"🟢 Open positions: {len(current)}", f"📁 Closed trades: {len(profits)}", f"🔄 Ledger BUYs: {len(buys)} • matched SELLs: {len(sells)}", "────────────────────────", f"Realized P/L: {_pnl_value(realized, 'SDA')}", f"Open P/L: {_pnl_value(open_pnl, 'SDA')}", f"Cumulative P/L: {_pnl_value(total, 'SDA')}", f"Win rate: {win_rate:.1f}%", f"Profit factor: {fmt_pf}", f"Avg win: {avg_win:+.2f} SDA", f"Avg loss: {avg_loss:+.2f} SDA", f"Best trade: {best:+.2f} SDA", f"Worst trade: {worst:+.2f} SDA", f"Open cost basis: {open_cost:.2f} SDA", "", "📜 RECENT REALIZED TRADES", "────────────────────────"]
        if not sells:
            lines.append("⚪ No matched real SELL trades yet")
        else:
            for tr in sorted(sells, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:15]:
                profit = dashboard.engine.num(tr.get("matched_proceeds_sda")) - dashboard.engine.num(tr.get("cost_basis_sda"))
                icon = "🟢" if profit > 0 else ("🔴" if profit < 0 else "⚪")
                label = tr.get("symbol") or str(tr.get("token", "UNKNOWN"))[:10]
                ts = str(tr.get("timestamp", ""))[:16].replace("T", " ")
                lines.append(f"{icon} {label} • {profit:+.2f} SDA • {ts}")
        lines += ["", "────────────────────────", "👁 READ-ONLY • Real wallet ledger"]
        return "\n".join(lines)

    def main_dashboard_v21(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        return "\n".join("🚨 " + line[2:] if line.startswith("🟡 ") and ": EMERGENCY SELL" in line else line for line in text.splitlines())

    def menu_keyboard_v21():
        keyboard = original_menu_keyboard()
        rows = keyboard.get("inline_keyboard", [])
        if not any(row and row[0].get("callback_data") == "REAL" for row in rows):
            rows.insert(2, [{"text": "💰 Real Trading", "callback_data": "REAL"}])
        return keyboard

    def handle_update_v21(update, state=None):
        cb = update.get("callback_query") or {}
        data = cb.get("data")
        if data not in ("REAL", "REAL_STATS"):
            return original_handle_update(update, state)
        state = state if state is not None else {"offset": 0}
        state["offset"] = max(int(state.get("offset", 0)), int(update.get("update_id", 0)) + 1)
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        configured_chat = __import__("os").environ.get("CHAT_ID")
        if configured_chat and str(chat_id) != str(configured_chat):
            dashboard.answer_callback(cb.get("id"), "Unauthorized")
            return state
        dashboard.answer_callback(cb.get("id"))
        if data == "REAL":
            keyboard = {"inline_keyboard": [[{"text": "📈 Statistics", "callback_data": "REAL_STATS"}], [{"text": "⬅️ Main dashboard", "callback_data": "MAIN"}]]}
            dashboard.edit(chat_id, message_id, real_trading_report(), keyboard)
        else:
            dashboard.edit(chat_id, message_id, real_statistics_report(), dashboard.back_keyboard())
        return state

    dashboard._position_recommendations = position_recommendations_v21
    dashboard.main_dashboard = main_dashboard_v21
    dashboard.real_trading_report = real_trading_report
    dashboard.real_statistics_report = real_statistics_report
    dashboard.menu_keyboard = menu_keyboard_v21
    dashboard.handle_update = handle_update_v21
    return dashboard
