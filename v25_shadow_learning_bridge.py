"""Runtime bridge for high-capacity V25 Shadow Learning v2.

Wraps the existing V25 update without changing its gate, exits, position sizing,
or MAX-WIN. Records V25-relevant market decisions across the analyzed universe
and evaluates them at 5/10/20/30 scanner passes.

The bridge is deliberately fail-safe for trading, but learning failures are
persisted to the shadow-learning state so a broken collector cannot remain
silently invisible.
"""
from __future__ import annotations

import time


def _diagnostic_save(learner, data, **updates):
    """Persist collector diagnostics without ever raising into V25 trading."""
    try:
        data.update(updates)
        learner._save(data)
    except Exception:
        pass


def _near_pass(c):
    """Identify useful threshold-adjacent rejects without changing the gate."""
    if c.get("bear", 0) >= 2:
        return False, 99
    checks = (
        c.get("pump_score", 0) >= 48.0,
        c.get("volume", 0) >= 200.0,
        c.get("trades", 0) >= 6.0,
        c.get("m1h", 0) >= 2.0,
        c.get("m15", 0) >= 0.0,
        c.get("m4h", 0) >= -1.0,
        c.get("flow", 0) >= -150.0,
    )
    misses = sum(not x for x in checks)
    return misses <= 2, misses


def patch(hunter):
    if getattr(hunter, "_sda_shadow_learning_v2_patched", False):
        return
    try:
        import v25_shadow_learning_v2 as learner
    except Exception as exc:
        print(f"V25 shadow learning import error: {type(exc).__name__}: {exc}")
        return
    original = getattr(hunter, "update", None)
    if not original:
        print("V25 shadow learning error: hunter.update is missing")
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
            data["last_run_at"] = time.time()
            data["last_error"] = None
            data["last_error_type"] = None
            data["last_tokens"] = len(tokens)
            data["last_candidates"] = 0
            data["last_recorded"] = 0
            data["last_near_pass"] = 0
            data["last_horizon_prices"] = 0
            learner._save(data)

            v25_state = state if isinstance(state, dict) else {}
            learned = hunter._learned_ready(v25_state)
            core = pe.load(pe.POSITIONS_FILE, {"positions": {}})
            core_positions = core.get("positions", {}) if isinstance(core, dict) else {}
            v25_positions = v25_state.get("positions", {}) if isinstance(v25_state, dict) else {}

            candidates = 0
            recorded = 0
            near_pass_count = 0
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
                if not (
                    c.get("pump_score", 0) >= 20
                    or c.get("score", 0) >= 35
                    or c.get("volume", 0) >= 150
                ):
                    continue

                candidates += 1
                allowed, reasons = hunter._gate(c, learned)
                is_near, near_misses = _near_pass(c) if not allowed else (False, 0)
                try:
                    symbol = scanner.lbl(address, meta) if hasattr(scanner, "lbl") else address
                except Exception:
                    symbol = address
                candidate = dict(c)
                candidate["address"] = address
                candidate["symbol"] = symbol
                candidate["near_pass"] = bool(is_near)
                candidate["near_pass_misses"] = int(near_misses)
                learner.record_observation(
                    candidate,
                    lane=("V25_PASS" if allowed else "V25_NEAR_PASS" if is_near else "V25_REJECT"),
                    allowed=allowed,
                    reasons=reasons,
                    scan=scan,
                    price=price,
                )
                recorded += 1
                if is_near:
                    near_pass_count += 1
                if recorded % 25 == 0:
                    _diagnostic_save(
                        learner,
                        data,
                        last_candidates=candidates,
                        last_recorded=recorded,
                        last_near_pass=near_pass_count,
                    )

            _diagnostic_save(
                learner,
                data,
                last_candidates=candidates,
                last_recorded=recorded,
                last_near_pass=near_pass_count,
            )

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
            try:
                import v25_shadow_learning_summary as summary_scanner
                summary_scanner.write_summary()
            except Exception as summary_exc:
                print(f"V25 shadow summary error: {type(summary_exc).__name__}: {summary_exc}")
            _diagnostic_save(
                learner,
                data,
                last_horizon_prices=len(prices),
                last_error=None,
                last_error_type=None,
                last_completed_at=time.time(),
            )
        except Exception as exc:
            try:
                data = learner._load()
                data["last_error"] = f"{type(exc).__name__}: {exc}"
                data["last_error_type"] = type(exc).__name__
                data["last_error_at"] = time.time()
                learner._save(data)
            except Exception:
                print(f"V25 shadow learning error: {type(exc).__name__}: {exc}")
        return state

    hunter.update = wrapped_update
    hunter._sda_shadow_learning_v2_patched = True
