"""Core V25 runtime compatibility/tuning logic.

MAX-WIN is untouched. V25 remains isolated and paper-only.
"""

def patch():
    import v25_paper_hunter as hunter
    import telegram_dashboard as dashboard

    original_gate = getattr(hunter, "_gate", None)
    if original_gate and not getattr(hunter, "_sda_warmup_gate_patched", False):
        def warmup_gate(c, learned):
            allowed, reasons = original_gate(c, learned)
            if allowed:
                return True, []
            early = (
                c["volume"] >= 250 and c["m1h"] >= 8.0 and c["m15"] >= 1.0 and
                c["accel"] >= 5.0 and c["bear"] < 2 and
                c["expected_mae"] >= -3.0 and c["expected_mfe"] >= 3.0 and
                c["ev"] >= -0.35 and c["pump_score"] >= 40.0 and c["p10"] >= 0.05
            )
            if early:
                return True, ["EARLY-PUMP lane: explosive momentum/acceleration"]
            quality = (
                c["volume"] >= 250 and c["trades"] >= 4 and c["score"] >= 40.0 and
                c["bear"] < 2 and c["expected_mae"] >= -4.0 and
                c["expected_mfe"] >= 3.0 and c["ev"] >= 0.0 and c["p10"] >= 0.08 and
                (c["m1h"] >= 0.0 or c["flow"] > 0.0 or c["accel"] >= 2.0)
            )
            if quality:
                return True, ["QUALITY-PUMP lane: positive EV/controlled downside"]
            return False, reasons
        hunter._gate = warmup_gate
        hunter._sda_warmup_gate_patched = True

    try:
        import dashboard_v23_patch as v23
        if not getattr(v23, "_sda_decision_identity_patched", False):
            original_decision = v23._decision
            def decision_with_identity(address, analysis, ws):
                d = original_decision(address, analysis, ws)
                if isinstance(d, dict):
                    d = dict(d)
                    d["address"] = str(address or "").lower()
                    d["_market_analysis"] = analysis if isinstance(analysis, dict) else {}
                return d
            v23._decision = decision_with_identity
            v23._sda_decision_identity_patched = True

        def dashboard_gate(d, canonical_volume=None):
            d = d or {}
            market_an = d.get("_market_analysis")
            if not isinstance(market_an, dict) or not market_an:
                data = d.get("data") or {}
                market_an = data.get("analysis") if isinstance(data.get("analysis"), dict) else data
            address = str(d.get("address") or "").lower()
            ws = dashboard.load("whale_data.json", {})
            c = hunter._features(__import__("main"), address, market_an, ws)
            if canonical_volume is not None and canonical_volume > 0:
                c["volume"] = canonical_volume
            learned = hunter._learned_ready(v23._v25_state())
            return hunter._gate(c, learned)

        v23._v25_gate = dashboard_gate
        v23._gate_reasons = lambda d, canonical_volume=None: list(dashboard_gate(d, canonical_volume)[1])

        old_debug = getattr(dashboard, "market_debug_report", None)
        if old_debug and not getattr(dashboard, "_sda_v25_horizon_patched", False):
            def debug_synced(snapshot=None):
                text = old_debug(snapshot)
                return text.replace(
                    "V25: shadow pump hunter • max 3 open • 50 SDA • 6h horizon",
                    "V25: shadow pump hunter • max 3 open • 50 SDA • 24h safety exit",
                )
            dashboard.market_debug_report = debug_synced
            dashboard._sda_v25_horizon_patched = True

        old_paper_report = getattr(dashboard, "paper_statistics_report", None)
        if old_paper_report and not getattr(dashboard, "_sda_v25_combined_stats_patched", False):
            def paper_statistics_with_v25(*args, **kwargs):
                base = old_paper_report(*args, **kwargs)
                try:
                    state = dashboard.load("v25_learner_state.json", {})
                    hs = state.get("paper_hunter", {}) if isinstance(state, dict) else {}
                    positions = hs.get("positions", {}) if isinstance(hs, dict) else {}
                    closed = hs.get("closed_trades", []) if isinstance(hs, dict) else []
                    market = dashboard.load("market_data.json", {"tokens": {}})
                    tokens = market.get("tokens", {}) if isinstance(market, dict) else {}
                    realized = sum(float(x.get("closed_profit_sda") or 0) for x in closed if isinstance(x, dict))
                    open_pnl = 0.0; invested = 0.0; rows = []
                    for address, pos in positions.items():
                        if not isinstance(pos, dict): continue
                        inv = float(pos.get("investment_sda") or 50.0); invested += inv
                        td = tokens.get(str(address).lower(), {}); an = dashboard._analysis(td) if isinstance(td, dict) else {}
                        entry = float(pos.get("entry_price") or 0); cur = float(an.get("price_in_sda") or 0)
                        pnl = inv * (cur / entry) * .999 * .99 - inv * 1.01 if entry > 0 and cur > 0 else 0.0
                        open_pnl += pnl; roi = (cur / entry - 1) * 100 if entry > 0 and cur > 0 else 0.0
                        rows.append((str(pos.get("opened_at", "")), pos.get("symbol") or address, pnl, roi, inv))
                    cumulative = realized + open_pnl
                    lines = ["", "🟣 V25 PUMP-HUNTER • SEPARATE PAPER BOOK", "────────────────────────", f"Open positions: {len(positions)}", f"Closed V25 trades: {len(closed)}", f"Realized P/L: {realized:+.2f} SDA", f"Open P/L: {open_pnl:+.2f} SDA", f"Cumulative P/L: {cumulative:+.2f} SDA", f"Current invested: {invested:.2f} SDA", "", "📌 V25 CURRENT POSITIONS"]
                    if not rows: lines.append("⚪ No open V25 positions")
                    else:
                        for _, symbol, pnl, roi, inv in sorted(rows, reverse=True):
                            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                            lines.append(f"{icon} {symbol} • P/L {pnl:+.2f} SDA ({roi:+.2f}%) • {inv:.0f} SDA")
                    main_state = dashboard.load("positions.json", {"positions": {}})
                    main_open = len((main_state.get("positions", {}) or {})) if isinstance(main_state, dict) else 0
                    lines += ["", f"📊 COMBINED PAPER OPEN: {main_open + len(positions)}"]
                    return base + "\n" + "\n".join(lines)
                except Exception:
                    return base
            dashboard.paper_statistics_report = paper_statistics_with_v25
            dashboard.paper_report = paper_statistics_with_v25
            dashboard._sda_v25_combined_stats_patched = True
    except Exception:
        pass

    # Install the observational learner only after the established V25 patching.
    # It wraps V25 update() and is fail-safe: learning can never affect trading.
    try:
        import v25_shadow_learning_bridge as shadow_bridge
        shadow_bridge.patch(hunter)
    except Exception as exc:
        print(f"V25 shadow learning bridge error: {exc}")
    return hunter
