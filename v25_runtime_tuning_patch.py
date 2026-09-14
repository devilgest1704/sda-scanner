"""Runtime compatibility/tuning layer for V25 PUMP-HUNTER.

MAX-WIN is untouched. V25 remains isolated and paper-only.  Warm-up now has
an EARLY-PUMP lane so explosive moves can be captured before flow/trade counts
fully confirm, while keeping strict downside and technical safeguards.
"""

def patch():
    import v25_paper_hunter as hunter
    import telegram_dashboard as dashboard

    original_gate = getattr(hunter, "_gate", None)
    if original_gate and not getattr(hunter, "_sda_warmup_gate_patched", False):
        def warmup_gate(c, learned):
            if learned:
                return original_gate(c, learned)

            # Standard warm-up confirmation lane.
            reasons = []
            if c["pump_score"] < 45: reasons.append(f"pump score {c['pump_score']:.0f}<45")
            if c["volume"] < 250: reasons.append(f"volume {c['volume']:.0f}<250")
            if c["trades"] < 5: reasons.append(f"trades {c['trades']:.0f}<5")
            if c["m1h"] < 2: reasons.append(f"M1H {c['m1h']:+.1f}%<+2.0%")
            if c["m15"] < 0: reasons.append(f"M15 {c['m15']:+.1f}%<0")
            if c["m4h"] < 0: reasons.append(f"M4H {c['m4h']:+.1f}%<0")
            if c["flow"] <= 0: reasons.append(f"flow {c['flow']:+.0f}<=0")
            if c["bear"] >= 2: reasons.append(f"technical bear {c['bear']}>=2")
            if c["expected_mae"] < -3: reasons.append(f"expected MAE {c['expected_mae']:+.1f}%<-3.0%")
            if c["expected_mfe"] < 4: reasons.append(f"expected MFE {c['expected_mfe']:+.1f}%<+4.0%")
            if c["ev"] < -0.15: reasons.append(f"learner EV {c['ev']:+.2f}<-0.15")
            if not reasons:
                return True, []

            # EARLY-PUMP lane: prioritize acceleration and explosive momentum.
            # It deliberately tolerates neutral/slightly negative flow because
            # flow often confirms after the price impulse. It still requires
            # liquidity, clean technicals, controlled downside and evidence of
            # a real short-term impulse. This is paper-only during warm-up.
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
