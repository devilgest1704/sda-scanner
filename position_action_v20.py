"""Shared V20/V21 position-action policy for Telegram display."""
from strategy_v21 import patch_engine, technical_sell_confirmed, evaluate_exit


def patch_dashboard(dashboard):
    original_main_dashboard = dashboard.main_dashboard

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
            # Wallet-only holdings without a reliable cost basis are not actionable.
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
                action = "EMERGENCY SELL"
                reason = "V21 emergency: ROI ≤ -15%, score <35, negative momentum + flow"
            elif weak >= 2 and not tp1_hit:
                action = "PARTIAL SELL"
                reason = "V21 weakening confirmed 2 times"
            elif neg >= 3:
                action = "SELL / EXIT"
                reason = "V21 negative exit confirmed 3 times"
            elif exit_signal["weakening"]:
                action = "HOLD / WATCH"
                reason = f"weakening signal {weak}/2 confirmations"
            elif exit_signal["negative"]:
                action = "HOLD / WATCH"
                reason = f"negative exit signal {neg}/3 confirmations"
            elif score >= 70 and m1h > 0 and flow1 > 0:
                action = "HOLD / TRAIL"
                reason = "positive trend and SDA flow"
            else:
                action = "HOLD / WATCH"
                reason = "no confirmed exit condition"

            rows.append({"token": token, "symbol": pf.get("symbol") or dashboard.engine.lbl(token, meta), "action": action, "reason": reason, "pnl_pct": pnl_raw, "pnl_sda": pf.get("unrealized_pnl_sda"), "score": score, "m1h": m1h, "flow_1h": flow1})

        order = {"EMERGENCY SELL": 0, "SELL / EXIT": 1, "PARTIAL SELL": 2, "HOLD / TRAIL": 3, "HOLD / WATCH": 4}
        return sorted(rows, key=lambda x: (order.get(x.get("action"), 9), -dashboard.engine.num(x.get("score"))))

    def main_dashboard_v21(*args, **kwargs):
        text = original_main_dashboard(*args, **kwargs)
        lines = []
        for line in text.splitlines():
            if line.startswith("🟡 ") and ": EMERGENCY SELL" in line:
                line = "🚨 " + line[2:]
            lines.append(line)
        return "\n".join(lines)

    dashboard._position_recommendations = position_recommendations_v21
    dashboard.main_dashboard = main_dashboard_v21
    return dashboard
