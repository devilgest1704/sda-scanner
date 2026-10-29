# User-requested experimental TP4 PAPER trial

The user explicitly requested moving Shadow TP4 rules into the main PAPER lane on 2026-10-29. This is an experimental manual change, not an evidence-qualified automatic promotion. One positive Shadow result does not establish profitability.

The scanner workflow enables SDA_PAPER_TP4=1. New entries require strict temporal recovery (the continuation and unconfirmed momentum lanes do not qualify). Existing MACD veto, loss circuit, cooldown and entry safeguards remain, so entry parity with Shadow is not guaranteed. New positions use 50 SDA principal and carry paper_strategy=temporal_recovery_tp4. Only tagged positions use raw observed quote thresholds: STOP -2.5%, TAKE +4%, trail 3.5% after 15 minutes, TIME 4 hours. Exit observations must be at most 300 seconds old. Legacy positions retain their original exits.

The main engine continues charging its existing 1% fee and 0.1% slippage per side. Its accounting differs slightly from the Shadow combined-friction formula; no Shadow profits or positions are imported. Historical main PAPER losses and statistics remain intact. New closed trades are labelled TP4 TAKE/STOP/TRAIL/TIME in the existing PAPER history. No actual token transaction or sellability confirmation is introduced. Real trading remains disabled.

Rollback: set SDA_PAPER_TP4=0 for future entries; existing tagged positions retain their frozen TP4 exits. Activation requires a new scanner worker using the merged workflow and source.
