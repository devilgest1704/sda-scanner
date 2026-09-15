"""Runtime bridge for high-capacity V25 Shadow Learning v2.

Wraps the existing V25 update without changing its gate, exits, position sizing,
or MAX-WIN. Records V25-relevant market decisions across the analyzed universe
and evaluates them at 5/10/20/30 scanner passes.
"""
from __future__ import annotations


def patch(hunter):
    if getattr(hunter, "_sda_shadow_learning_v2_patched", False):
        return
    try:
        import v25_shadow_learning_v2 as learner
    except Exception:
        return
    original = getattr(hunter, "update", None)
    if not original:
        return

    def wrapped_update(*args, **kwargs):
        state = original(*args, **kwargs)
        try:
            import main as scanner
            pe = scanner._paper
            md = pe.load(pe.MARKET_FILE, {"tokens": {}})
            tokens = md.get("tokens", {}) if isinstance(md, dict) else {}
            whale = pe.load(pe.WHALE_FILE, {})
            meta = pe.load(pe.META_FILE, {})

            data = learner._load()
            scan = int(data.get("scan_counter") or 0) + 1
            data["scan_counter"] = scan
            learner._save(data)

            # High-throughput shadow dataset: do not wait for a V25 WATCH to
            # become a completed trade. Record every V25-relevant decision
            # from the analyzed universe, including rejected candidates.
            v25_state = state if isinstance(state, dict) else {}
            learned = hunter._learned_ready(v25_state)
            core = pe.load(pe.POSITIONS_FILE, {"positions": {}})
            core_positions = core.get("positions", {}) if isinstance(core, dict) else {}
            v25_positions = v25_state.get("positions", {}) if isinstance(v25_state, dict) else {}

            for raw, td in tokens.items():
                address = str(raw).lower()
                if address in core_positions or address in v25_positions:
                    continue
                if not isinstance(td, dict):
                    continue
                an = hunter._analysis(td)
                try:
                    price = float(an.get("price_in_sda") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price <= 0:
                    continue

                c = hunter._features(scanner, address, an, whale)
                # Keep storage focused on signals where V25 could plausibly
                # matter. This is deliberately much broader than WATCH.
                if not (
                    c.get("pump_score", 0) >= 20
                    or c.get("score", 0) >= 35
                    or c.get("volume", 0) >= 150
                ):
                    continue

                allowed, reasons = hunter._gate(c, learned)
                try:
                    symbol = scanner.lbl(address, meta) if hasattr(scanner, "lbl") else address
                except Exception:
                    symbol = address
                candidate = dict(c)
                candidate["address"] = address
                candidate["symbol"] = symbol
                learner.record_observation(
                    candidate,
                    lane="V25_PASS" if allowed else "V25_REJECT",
                    allowed=allowed,
                    reasons=reasons,
                    scan=scan,
                    price=price,
                )

            # Evaluate all prior observations against the current market snapshot.
            prices = {}
            for raw, td in tokens.items():
                if not isinstance(td, dict):
                    continue
                an = hunter._analysis(td)
                try:
                    p = float(an.get("price_in_sda") or 0)
                except (TypeError, ValueError):
                    p = 0.0
                if p > 0:
                    prices[str(raw).lower()] = p
            learner.update_horizons(prices, scan)
        except Exception:
            # Learning is strictly observational and must never break V25 trading.
            pass
        return state

    hunter.update = wrapped_update
    hunter._sda_shadow_learning_v2_patched = True
