"""Runtime compatibility/tuning layer for V25 PUMP-HUNTER.

MAX-WIN is untouched. V25 remains isolated and paper-only. Warm-up and
learned mode both have an EARLY-PUMP lane so explosive moves can be captured
before slower flow/trade confirmation, while keeping strict downside and
technical safeguards.
"""

def patch():
    import v25_paper_hunter as hunter
    import telegram_dashboard as dashboard

    original_gate = getattr(hunter, "_gate", None)
    if original_gate and not getattr(hunter, "_sda_warmup_gate_patched", False):
        def warmup_gate(c, learned):
            # First evaluate the normal V25 gate. This remains the preferred
            # path because it has stronger confirmation.
            allowed, reasons = original_gate(c, learned)
            if allowed:
                return True, []

            # EARLY-PUMP lane is intentionally available even after learning
            # becomes ready. The live samples showed that price acceleration
            # can precede flow/trade-count confirmation, while the bad pump
            # examples were characterized by materially worse MAE/EV. Keep
            # those downside/quality filters hard.
            early = (
                c["volume"] >= 250 and
                c["m1h"] >= 8.0 and
                c["m15"] >= 1.0 and
                c["accel"] >= 5.0 and
                c["bear"] < 2 and
                c["expected_mae"] >= -3.0 and
                c["expected_mfe"] >= 3.0 and
                c["ev"] >= -0.35 and
                c["pump_score"] >= 40.0 and
                c["p10"] >= 0.05
            )
            if early:
                return True, ["EARLY-PUMP lane: explosive momentum/acceleration"]

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
    except Exception:
        pass
    return hunter
