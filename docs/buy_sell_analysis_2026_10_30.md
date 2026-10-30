# BUY / SELL audit, 2026-10-30

Pinned source: 9a9fd0d135ca12a820c1d0e57af8e7dd384988ee. Full input SHA256 hashes accompany the machine-readable report. Reproduce with `python -m tools.buy_sell_audit`.

The current main PAPER ledger contains 597 closes, net -1586.094973 SDA and one open position. The append-only audit archive contains 603 rows; archive-only records are not added to current accounting. Every current closed trade is counted; missing feature/quote evidence is reported rather than invented.

BUY: 5000 frozen observations yield 933 causally ordered, token-decorrelated 60–75 minute outcomes. A 60/20/20 chronological split with a 75-minute purge gives 559/184/187 observations. Of 629 prebounded one/two-feature momentum/flow/liquidity rules, none passes train support, breadth, positive net and profit-factor requirements. No validation/test result is used to select a second candidate.

SELL: 249 current trades retain price samples, only 113 have any per-sample quote timestamps. Split those 113 chronologically into 67/23/23 and search 135 fixed TAKE/STOP/trail/time combinations on TRAIN only. None qualifies. The largest apparent train profit is +7.675036 SDA (TAKE3%, STOP4%, trail2.5%, 1h), but only 13 trades qualify, 19.4% coverage; 48 paths have unsafe quote evidence and 5 are truncated at the incumbent exit. This is not evidence for deployment. Unobserved future prices are never extrapolated. Across all current trades TP4 has only 23 replayable closes, net -8.804444 SDA at normalized 50 SDA principal. This is a selected, incomplete cohort, not a portfolio backtest or proof of future losses.

## Concrete runtime defect repaired

The engine invoked the V30/TP4 exit callback, then independently executed legacy TP1/TP2/SL on surviving positions. The second loop could apply stale-price exits after the strategy had rejected that quote, or silently change remaining_fraction without a ledger partial record. Strategy-managed positions now skip that legacy loop. Untagged legacy positions retain existing behavior; past ledger rows are not rewritten. This fixes execution ownership, not profitability. The prior TP4 deployment did not cover this full-runner interaction.

## Decision

No defensible profitable BUY/SELL filter was identified. Keep the explicitly requested experimental TP4 rule rather than deploy a cherry-picked historical winner. Deploy only the exit-ownership correction and regression tests. Historical main PAPER losses remain visible. No real-trading change or execution/sellability claim. Further research needs forward evidence with complete fresh quote paths; the three currently requested milestones remain reviews, not proof of fills.
