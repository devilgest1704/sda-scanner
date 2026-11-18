# REAL BUY pipeline separation — NOT READY

## Status

This branch is a review/implementation gate only. It does **not** implement REAL/PAPER separation and must not be merged or deployed.

## Verified current coupling

- `run_scanner_v18.sh` runs the PAPER engine and then calls `real_paper_bridge.reconcile()`.
- `real_paper_bridge.py` reads `positions.json` through `PAPER_FILE` / `_paper_positions()` and derives REAL BUYs from newly observed PAPER positions.
- `v30_pump_hunter.decision()` currently reads PAPER-owned state through `load_state()`, `cooldown_active()`, `paper_entry_risk_gate()`, `temporal_recovery_buy` (whose default prior source is `forward_shadow_state.json`), PAPER quote-quality state and PAPER champion configuration.
- `tools.temporal_recovery_buy.evaluate_paper/evaluate_tp4` accept an explicit `previous` signal, so the temporal rules can be reused without the default Shadow journal, but a REAL-owned signal history must be persisted and passed explicitly.
- `real_trader.safety_check(position_sda, state)` already checks REAL positions, pending buys, position count, capital cap and daily realized-loss limit. `execute_buy()` persists pending BUY intent before transaction broadcast. This must remain the final execution boundary.

## Required implementation before READY

1. Extract the existing market-only scoring, lifecycle, entry thresholds, V31 freshness/activity checks, MACD veto and quote-quality rules into one pure shared evaluator. Keep the numeric thresholds and temporal-recovery rules unchanged.
2. Keep PAPER evaluation on its existing state adapter and preserve current PAPER behavior.
3. Add a REAL adapter with a dedicated, schema-versioned `real_buy_signal_state.json`. It must persist per-token temporal observations, signal history, cooldowns, risk-circuit history, consumed signal IDs and diagnostics using atomic writes. Missing, malformed or unsupported state must block REAL BUY.
4. Read REAL positions and pending BUYs only from `real_trader.load_state()` / the REAL accounting snapshot. Calculate capacity from the REAL 10-position and 300-SDA limits and live wallet balance; do not consult PAPER capacity.
5. Do not read `positions.json`, `forward_shadow_state.json`, PAPER cooldown, PAPER loss circuit, PAPER optimizer runtime or newly seen PAPER positions anywhere in the REAL signal path.
6. Integrate the REAL signal adapter in the scanner independently of the PAPER engine. Run `real_trader.safety_check()` immediately before `execute_buy()`. Keep SELL and manual wallet reconciliation paths unchanged.
7. Add the ten requested isolation/restart/dry-run integration tests. The executor test double must stop at WOULD EXECUTE and must not sign or broadcast.

## Acceptance tests

The ten scenarios from the implementation request are mandatory. In particular, PAPER-full/cooldown/held-token/loss-circuit states must not affect a healthy REAL candidate, while REAL cooldown, 10/10 positions and insufficient REAL capital must block it.

## Validation required

Run the complete test suite, lint, typecheck, syntax compilation and build in a checkout of this branch. Confirm the end-to-end executor path with a mock that reports `WOULD EXECUTE`; no private key, signing, transaction submission or broadcast is permitted.

## Production safety

- Do not merge this PR.
- Do not change the running production worker or its code pin.
- Do not enable or dispatch REAL BUY execution from this branch.
- No REAL BUY or SELL is authorized by this PR.

**Current state: NOT READY — documentation only; implementation and validation are outstanding.**
