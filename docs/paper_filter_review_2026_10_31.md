# PAPER recovery filter review — 2026-10-31

Source checkout fa75564; candidate archive source 2026-10-31T19:01:53Z. All 598 current closes total -1587.709392 SDA. The separate 604-row append-only archive is audit context, not extra current P/L.

Re-ran 629 bounded BUY rules on 5000 frozen observations (988 decorrelated usable outcomes; purged chronological split 588/197/198). None qualified on training. Re-ran 135 exit rules: none qualified; observed replay coverage is insufficient. See the two dated evidence JSON files. No claim of exhaustive search or future profit.

## Main PAPER change

New TP4 entries keep the stale-to-fresh recovery requirement and every existing age, quote-continuity, activity, cooldown, MACD and risk gate. Reject m15 >8% (the existing fresh-momentum lane already uses this cap), and reject missing/nonfinite/nonpositive 15-minute net buy flow. Store flow_15m in frozen entry metrics going forward. Independent SHADOW journals, legacy PAPER admission, exits, stake and real trading are unchanged.

The cap alone rejects 2/8 historical confirmed recovery admissions, whose observed incumbent results sum to -7.645709 SDA. The other 6 still total -19.415605 SDA. This is selected-cohort attribution, not a TP4 counterfactual portfolio backtest, validation win or forecast: earlier trades used different exits. Historical entry metrics omit flow_15m, so the joint new filter cannot be reconstructed honestly. No missing flow values are imputed as passing.

This defensive experimental change does not increase BUY frequency. Latest diagnostic has no fresh token with >=8 trades/hour; forcing BUY would require relaxing protections without profitable evidence. New post-change positions are attributable through paper_filter_revision=tp4_flow_antichase_v1. Existing positions/history are not rewritten and no automatic promotion is introduced.
