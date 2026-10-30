# PAPER quote continuity and inclusive TP4 statistics

User-authorized adjustment based on 2026-10-30 analysis. Applies to new TP4 PAPER buys only. No promise of profitability and no SELL execution proof.

A new entry must retain every existing temporal recovery/MACD/risk gate AND have a fresh current quote (<=300s), at least three distinct priced transaction timestamps in the trailing 15 minutes, and no gap above 600s over that full window, including its leading/trailing boundaries. Current quote timestamp must equal the latest observed history quote. History is bound to the token from the current runtime market snapshot; stale/missing snapshots fail closed. Duplicate timestamps, future/invalid prices do not count. A lone fresh transaction after inactivity cannot qualify. These are fixed data-quality criteria, not profit-optimized thresholds. Missing market evidence is UNKNOWN, never zero liquidity or confirmed unsellability.

Closed main PAPER TP4 statistics include ALL financial results, regardless of quote quality. Separate valid/invalid/unknown counts accompany realized P/L, realized drawdown, unique tokens and open principal at risk. Open principal at risk is exposure, not an assumed realized loss or mark-to-market value. Every simulated SELL remains unverified as an executable fill. Legacy closes without new frozen entry evidence remain quality UNKNOWN; past ledgers are not modified. The panel is rendered in the existing PAPER statistics page and persisted in paper_stats.json under tp4_paper.

The runtime freezes entry continuity evidence and maximum inter-quote gap on new positions. Closed data quality requires frozen passing entry evidence and no holding quote gap above 30 minutes, independently from net P/L. All results stay in cohort P/L. Existing TP4 TAKE4/STOP2.5/trail3.5/4h exits, 50 SDA principal, max positions, cooldown, fees and 24h loss budget remain unchanged. The 15-valid-close flag is descriptive and cannot promote a strategy.

Historical check: the repository raw market_data snapshot predates the two TP4 main entries, and the rolling candidate archive does not preserve sufficient pre-entry evidence for them. They cannot honestly be replayed against this gate; no saved-profit estimate is claimed. See paper_quote_quality_history_2026_10_30.json. Prospective evidence is frozen for subsequent auditing.

Regression cases cover regular/sparse/burst/duplicate/future/mismatched quotes, token binding, stale snapshots and inclusion of invalid/unknown losses in net/drawdown. Full engine tests retain ownership of exits.
