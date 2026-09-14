"""Runtime bridge for V25 Shadow Learning v2.

Wraps the existing V25 update without changing its gate, exits, position sizing,
or MAX-WIN. It records WATCH observations and evaluates them at 5/10/20/30
scanner passes using the live market price.
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
            # Persistent scan counter lives in the V2 file, independent of V25 state.
            data = learner._load()
            scan = int(data.get("scan_counter") or 0) + 1
            data["scan_counter"] = scan
            learner._save(data)

            # Record every currently tracked WATCH as a shadow entry snapshot.
            v25 = state if isinstance(state, dict) else {}
            watches = v25.get("watch", {}) if isinstance(v25, dict) else {}
            for address, row in watches.items():
                if not isinstance(row, dict):
                    continue
                price = float(row.get("last_price") or row.get("entry_price") or 0)
                metrics = row.get("entry_metrics") if isinstance(row.get("entry_metrics"), dict) else {}
                candidate = dict(metrics)
                candidate.update(row)
                candidate["address"] = address
                candidate["symbol"] = row.get("symbol")
                learner.record_observation(
                    candidate,
                    lane="WATCH",
                    allowed=False,
                    reasons=list(row.get("gate_reasons") or []),
                    scan=scan,
                    price=price,
                )

            # Evaluate all prior observations against the current market snapshot.
            prices = {}
            for raw, td in tokens.items():
                if not isinstance(td, dict):
                    continue
                an = td.get("analysis", td)
                if isinstance(an, dict):
                    try:
                        p = float(an.get("price_in_sda") or 0)
                    except (TypeError, ValueError):
                        p = 0
                    if p > 0:
                        prices[str(raw).lower()] = p
            learner.update_horizons(prices, scan)
        except Exception:
            # Learning is strictly observational and must never break V25 trading.
            pass
        return state

    hunter.update = wrapped_update
    hunter._sda_shadow_learning_v2_patched = True
