# V31 PROFIT-FIRST — experimental validation protocol

Status: SHADOW_ONLY; NOT connected to BUY/SELL. Thresholds are hypotheses, not fitted to trading outcomes.

## Historical inputs required
One row per completed trade: unique trade ID, token address, entry/exit UTC timestamps, entry/exit prices, position size SDA, net realized P/L SDA, exit reason, scanner version, and ENTRY-TIME snapshot of trades/vol/flow/buy_ratio/m15/m1/quality/data_age_sec. Capture fees and slippage if available.

The existing paper_stats.json is only an aggregate and cannot establish which filter would have worked. Current positions.json and market_data.json fetched via the GitHub file API did not expose usable trade records. Do not synthesize them.

## Validation
1. Reconcile individual trade count, wins, losses, total realized P/L against paper_stats.json.
2. Verify entry-time snapshots are timestamped at or before entry (no future leakage).
3. Sort by entry timestamp. Reserve the latest 30% as an untouched out-of-sample holdout; use earlier 70% only for candidate selection. Avoid overlapping trade leakage.
4. Compare baseline versus V31 on the same eligible trade opportunities. Report count, net SDA P/L, win rate, average win/loss, profit factor, max drawdown, and uncertainty. Include zero-trade outcomes explicitly.
5. Re-run with realistic execution fees, spread, slippage, liquidity constraints. Evaluate monthly stability and token concentration.
6. Keep SHADOW_ONLY until holdout performance is positive after costs with enough trades and acceptable drawdown. Never promise profitability.

## Commands
Run tests from repository root:
`python -m unittest discover -s tests -p 'test_v31_profit_gate.py'`

## Safety
Do not overwrite main, enable automated trading, or alter existing V30 behavior from this experiment.
