"""Single canonical Position Action policy for dashboard and Real Wallet."""
from copy import deepcopy
import re


def _action_icon(action):
    return {
        "EMERGENCY SELL": "🚨",
        "SELL / EXIT": "🔴",
        "PARTIAL SELL": "🟠",
        "HOLD / TRAIL": "🟢",
        "HOLD / WATCH": "🟡",
        "HOLD / NO COST BASIS": "🟡",
        "HOLD / MARKET DATA N/A": "🟡",
    }.get(action, "🟡")


def patch_dashboard(dashboard):
    if getattr(dashboard, "_sda_dashboard_consistency_patched", False):
        return dashboard

    engine = dashboard.engine
    original_recommendations = dashboard._position_recommendations
    original_merge_wallet = dashboard._merge_wallet_portfolio

    def _wallet_synced_portfolio(wallet, portfolio):
        result = deepcopy(portfolio) if isinstance(portfolio, dict) else {}
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

    def _resolve_token_data(md, token, pf, meta):
        tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
        candidates = [token]
        if isinstance(pf, dict):
            candidates += [pf.get("address"), pf.get("token_address")]
        for candidate in candidates:
            if not candidate:
                continue
            key = str(candidate).strip()
            if key in tokens:
                return key, dashboard._analysis(tokens.get(key, {}) or {})
            if key.lower() in tokens:
                return key.lower(), dashboard._analysis(tokens.get(key.lower(), {}) or {})

        symbol = str((pf or {}).get("symbol") or token).strip().upper()
        for address, value in tokens.items():
            if not isinstance(value, dict):
                continue
            analysis = dashboard._analysis(value)
            label = engine.lbl(address, meta)
            raw_symbol = value.get("symbol") or analysis.get("symbol")
            if str(label).split("/", 1)[0].strip().upper() == symbol or str(raw_symbol or "").strip().upper() == symbol:
                return str(address), analysis
        return "", {}

    def _canonical_recommendations(md, ws, meta, portfolio):
        """Canonical V21 Position Action policy used by every UI view."""
        wallet = dashboard.load("wallet_data.json", {})
        synced = _wallet_synced_portfolio(wallet, portfolio)
        current = synced.get("current", {}) if isinstance(synced, dict) else {}
        rows = []

        for token, pf in current.items():
            if not isinstance(pf, dict):
                continue
            market_key, analysis = _resolve_token_data(md, token, pf, meta)
            try:
                signal = engine.score(market_key or token, analysis, ws) if analysis else {}
            except Exception:
                signal = {}

            score_raw = signal.get("confidence")
            score = engine.num(score_raw) if score_raw is not None else None
            m1_raw = signal.get("m1h")
            flow_raw = signal.get("net_1h")
            m1h = engine.num(m1_raw) if m1_raw is not None else None
            flow1 = engine.num(flow_raw) if flow_raw is not None else None
            pnl_raw = pf.get("unrealized_pnl_pct")
            pnl = engine.num(pnl_raw) if pnl_raw is not None else None

            data_missing = not bool(analysis) or score is None or m1h is None or flow1 is None
            try:
                from strategy_v21 import technical_sell_confirmed, evaluate_exit
                bearish = technical_sell_confirmed(analysis) if analysis else False
                exit_signal = evaluate_exit(
                    pnl if pnl is not None else 0.0,
                    score if score is not None else 0.0,
                    m1h if m1h is not None else 0.0,
                    flow1 if flow1 is not None else 0.0,
                    bearish,
                )
            except Exception:
                bearish = False
                exit_signal = {}

            negative_evidence = sum((
                m1h is not None and m1h < 0,
                flow1 is not None and flow1 < 0,
                bearish,
                score is not None and score < 40,
            ))
            positive_evidence = sum((
                m1h is not None and m1h > 0,
                flow1 is not None and flow1 > 0,
                score is not None and score >= 70,
                not bearish,
            ))

            if exit_signal.get("emergency"):
                action = "EMERGENCY SELL"
                reason = "ROI ≤ -15% with weak score, momentum and SDA flow"
            elif pnl is not None and pnl <= -8 and score is not None and score < 35 and m1h is not None and m1h < 0 and flow1 is not None and flow1 < 0:
                action = "SELL / EXIT"
                reason = "loss > 8% + weak score + negative momentum + SDA flow"
            elif pnl is not None and pnl <= -4 and negative_evidence >= 3:
                action = "SELL / EXIT"
                reason = "loss > 4% with 3+ bearish signals"
            elif pnl is not None and pnl > 5 and score is not None and score < 40 and (m1h is not None and m1h < 0 or flow1 is not None and flow1 < 0) and bearish:
                action = "PARTIAL SELL"
                reason = "profit > 5% but trend/flow is weakening"
            elif pnl is not None and pnl > 10 and score is not None and score < 45 and negative_evidence >= 2:
                action = "PARTIAL SELL"
                reason = "profit > 10% with weakening market evidence"
            elif pnl is not None and pnl > 0 and positive_evidence >= 3 and score is not None and score >= 55:
                action = "HOLD / TRAIL"
                reason = "position profitable with supportive trend/flow"
            elif pnl is not None and pnl < 0 and negative_evidence >= 2:
                action = "HOLD / WATCH"
                reason = "loss with mixed-to-bearish evidence; monitor next scan"
            elif data_missing:
                action = "HOLD / MARKET DATA N/A"
                reason = "market data not resolved for this wallet token"
            else:
                action = "HOLD / WATCH"
                reason = "no confirmed exit condition"

            rows.append({
                "token": token,
                "address": market_key,
                "symbol": pf.get("symbol") or engine.lbl(market_key or token, meta),
                "action": action,
                "reason": reason,
                "pnl_pct": pnl_raw,
                "pnl_sda": pf.get("unrealized_pnl_sda"),
                "score": score,
                "m1h": m1h,
                "flow_1h": flow1,
            })

        # One ordering everywhere: best current SDA P/L first, unknown last.
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
        portfolio = _wallet_synced_portfolio(wallet, dashboard.load("portfolio_data.json", {}))
        wallet_view = dashboard._merge_wallet_portfolio(wallet, portfolio, md, ws, meta)
        rows = _canonical_recommendations(md, ws, meta, portfolio)

        lines = [wallet_view, "", "🧭 POSITION ACTION", "────────────────────────"]
        if not rows:
            lines.append("⚪ No actionable real positions")
        else:
            for row in rows:
                action = row.get("action")
                icon = _action_icon(action)
                pnl = "UNKNOWN" if row.get("pnl_sda") is None else f"{engine.num(row.get('pnl_sda')):+.2f} SDA"
                score = "N/A" if row.get("score") is None else f"{engine.num(row.get('score')):.0f}/100"
                lines.append(f"{icon} {row.get('symbol')}: {action} • P/L {pnl} • score {score}")
                lines.append(f"   {row.get('reason')}")
        lines += ["", "────────────────────────", "👁 READ-ONLY • No real order is executed"]
        return "\n".join(lines)

    dashboard._position_recommendations = _canonical_recommendations
    dashboard._merge_wallet_portfolio = _sort_wallet_view
    dashboard.real_trading_report = real_trading_report_canonical
    dashboard._sda_action_icon = _action_icon
    dashboard._sda_dashboard_consistency_patched = True
    return dashboard
