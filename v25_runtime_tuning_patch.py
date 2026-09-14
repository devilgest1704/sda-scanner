"""Runtime compatibility/tuning layer for V25 PUMP-HUNTER.

Keeps MAX-WIN untouched. During learner warm-up, allow lower-score but
low-downside pump structures so the hunter can collect useful samples and
capture early moves. Once learned, the original strict V25 gate remains in
force. The dashboard is patched to use the exact same gate and horizon text.
"""

def patch():
    import v25_paper_hunter as hunter

    original_gate = getattr(hunter, "_gate", None)
    if not original_gate or getattr(hunter, "_sda_warmup_gate_patched", False):
        return hunter

    def warmup_gate(c, learned):
        if learned:
            return original_gate(c, learned)

        reasons = []
        # Warm-up is deliberately permissive on score, but strict on
        # downside and evidence of an actual move. This is a paper learner:
        # it needs representative pump samples, not a second MAX-WIN gate.
        if c["pump_score"] < 45:
            reasons.append(f"pump score {c['pump_score']:.0f}<45")
        if c["volume"] < 250:
            reasons.append(f"volume {c['volume']:.0f}<250")
        if c["trades"] < 5:
            reasons.append(f"trades {c['trades']:.0f}<5")
        if c["m1h"] < 2:
            reasons.append(f"M1H {c['m1h']:+.1f}%<+2.0%")
        if c["m15"] < 0:
            reasons.append(f"M15 {c['m15']:+.1f}%<0")
        if c["m4h"] < 0:
            reasons.append(f"M4H {c['m4h']:+.1f}%<0")
        if c["flow"] <= 0:
            reasons.append(f"flow {c['flow']:+.0f}<=0")
        if c["bear"] >= 2:
            reasons.append(f"technical bear {c['bear']}>=2")
        if c["expected_mae"] < -3:
            reasons.append(f"expected MAE {c['expected_mae']:+.1f}%<-3.0%")
        if c["expected_mfe"] < 4:
            reasons.append(f"expected MFE {c['expected_mfe']:+.1f}%<+4.0%")
        if c["ev"] < -0.15:
            reasons.append(f"learner EV {c['ev']:+.2f}<-0.15")
        return not reasons, reasons

    hunter._gate = warmup_gate
    hunter._sda_warmup_gate_patched = True

    # dashboard_v23_patch contains a duplicate gate for presentation. Point
    # it at the exact execution gate so diagnostics cannot disagree with the
    # paper trader.
    try:
        import dashboard_v23_patch as v23
        def dashboard_gate(d, canonical_volume=None):
            data = (d or {}).get("data") or {}
            an = data.get("analysis") if isinstance(data.get("analysis"), dict) else data
            flow = an.get("flow", {}).get("1h", {}) if isinstance(an, dict) else {}
            score = v23._num((d or {}).get("score", data.get("buy_score", data.get("confidence"))))
            volume = v23._num(canonical_volume) if canonical_volume is not None else v23._volume_1h(an, data)
            trades = v23._num(data.get("trades_1h")) or v23._num(flow.get("buy_count")) + v23._num(flow.get("sell_count"))
            bear = int((d or {}).get("technical_bear", data.get("technical_bear")) or 0)
            # Build the same feature vector used by the execution engine.
            c = {
                "pump_score": score,
                "volume": volume,
                "trades": trades,
                "m1h": v23._num(data.get("m1h")),
                "m15": v23._num(data.get("m15")),
                "m4h": v23._num(data.get("m4h")),
                "flow": v23._num(data.get("net_1h")),
                "bear": bear,
                "expected_mae": v23._num((d or {}).get("v25", {}).get("expected_mae")),
                "expected_mfe": v23._num((d or {}).get("v25", {}).get("expected_mfe")),
                "ev": v23._num((d or {}).get("v25", {}).get("expected_pl_sda")),
                "accel": 999,
                "volume_ratio": 999,
                "trade_ratio": 999,
            }
            # The dashboard's original candidate score is not the pump score.
            # Prefer the actual hunter feature calculation when possible.
            try:
                import main as scanner
                c = hunter._features(scanner, str((d or {}).get("address") or "").lower(), an, {})
                if canonical_volume is not None and canonical_volume > 0:
                    c["volume"] = canonical_volume
            except Exception:
                pass
            try:
                state = v23._v25_state()
                learned = hunter._learned_ready(state)
            except Exception:
                learned = False
            return hunter._gate(c, learned)
        v23._v25_gate = dashboard_gate
        v23._gate_reasons = lambda d, canonical_volume=None: list(dashboard_gate(d, canonical_volume)[1])

        # Fix stale wording left by the older V25 reporting layer.
        old_debug = getattr(v23, "debug_v25", None)
        if old_debug:
            def debug_v25_synced(snapshot=None):
                text = old_debug(snapshot)
                return text.replace("V25: shadow pump hunter • max 3 open • 50 SDA • 6h horizon",
                                    "V25: shadow pump hunter • max 3 open • 50 SDA • 24h safety exit")
            v23.debug_v25 = debug_v25_synced
    except Exception:
        pass

    return hunter
