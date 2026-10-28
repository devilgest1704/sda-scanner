# Temporal recovery TP4: forward PAPER experiment

Research snapshot: 2026-10-28T17:28:31.969552+00:00. Source blob: a93df9cee83c75a9dc92fa451164fa179849ce9c.

## Decision

No strategy has proved deployable profitability. Temporal recovery is the most promising entry hypothesis among established arms with at least five valid closes, but has only 9 valid closes, 4 invalid closes and 69.23% quote coverage. Its +11.152863 SDA valid-quote model profit becomes -188.847137 SDA under a separate full-stake loss stress for invalid closes, or -238.847137 SDA including its one open 50 SDA position. Stress values are assumptions, not observed trading losses.

Implement a NEW independent forward-only temporal recovery TP4 arm. Do not promote it into the main PAPER account. Previous TP4 earlier-exit research improved training (45 same trades: -69.5713 to -41.9881 SDA) and exploratory holdout (20 trades: -40.4076 to -37.8971 SDA), but both remained negative. Joint profitability with temporal entries is UNPROVEN. The cost models in those two studies differ, so their effects cannot be added.

## Forward model

BUY: same temporal recovery eligibility as parent: previous strong IGNITION/CONFIRMATION score >=68 and impulse >=8, quote age >300 and <=900 seconds; a newer quote within 10 minutes and current age <=300 seconds; current score >=64, quality >=60, impulse >=4, buy/sell ratio >=1.2, trades >=12, hourly volume >=500 SDA, net flow >=150 SDA, hourly momentum 0..12%, M15 >=0.8% for IGNITION or >=1.5% for CONFIRMATION. Market and candidate price must agree within 1.5%.

SELL at a subsequent fresh observed quote: gross stop -2.5%, gross take +4%, trailing -3.5% from observed peak after 15 minutes, or 4-hour limit. No interpolation or assumed fill at the threshold. Fixed 50 SDA stake, 1.1% friction each side, maximum four positions, six-hour entry cooldown, maximum one new entry per scanner cycle. Gapped/late/jumping prices remain excluded from positive learning; stale 8h timeout retains its conservative writeoff. Existing generic Shadow rules are not identical to the main PAPER risk circuit or its actual exits.

Each arm owns its positions. Different exits can cause portfolio capacity and later entries to diverge; matched address/opened_at pairs describe only their common entries. Neither paired results nor cumulative totals authorize promotion. No historical parent trades or positions are imported into the new journal.

## Snapshot comparison

Different inception dates, exposure and entries prevent causal ranking of these totals. All are quote models, not executable fills. Invalid full-stake stress is independent of the P/L recorded at an invalid quote.

| Policy | Valid / closed | Valid quote net SDA | Invalid-close stake stress SDA | Open positions |
|---|---:|---:|---:|---:|
| baseline | 135 / 192 | -178.6202 | -2850.00 | 1 |
| quality | 131 / 187 | -163.3748 | -2800.00 | 1 |
| momentum | 108 / 149 | -133.3408 | -2050.00 | 1 |
| selective | 85 / 105 | -131.0910 | -1000.00 | 0 |
| score | 104 / 137 | -106.2358 | -1650.00 | 1 |
| quality_plus | 130 / 182 | -163.9183 | -2600.00 | 1 |
| research_watch | 64 / 85 | -94.4576 | -1050.00 | 0 |
| research_watch_strict | 22 / 26 | -49.8612 | -200.00 | 0 |
| research_macd_antipump | 2 / 5 | -5.1348 | -150.00 | 0 |
| research_macd_confirmation | 0 / 0 | 0.0000 | 0.00 | 0 |
| research_breakout_volume_trend | 62 / 73 | -81.5588 | -550.00 | 1 |
| research_trend_runner | 84 / 158 | -158.3769 | -3700.00 | 2 |
| research_evo_55b7f0f909 | 13 / 16 | -1.0975 | -150.00 | 0 |
| research_evo_5763c66185 | 2 / 3 | 0.2848 | -50.00 | 0 |
| research_sparse_volume_ignition | 3 / 6 | -6.4059 | -150.00 | 1 |
| research_temporal_recovery | 9 / 13 | 11.1529 | -200.00 | 1 |
| research_v33_flow_continuation | 37 / 51 | -42.9005 | -700.00 | 1 |
| research_v33_cost_aware_breakout | 17 / 27 | -18.1605 | -500.00 | 1 |
| research_evo_3ee2f045c1 | 19 / 26 | -0.7410 | -350.00 | 0 |
| research_evo_b0123b22a0 | 13 / 14 | -24.9037 | -50.00 | 0 |
| research_evo_707ea0a87d | 12 / 23 | -13.8456 | -550.00 | 0 |
| research_loss_cooldown_6h | 2 / 3 | -4.7327 | -50.00 | 1 |
| research_loss_cooldown_12h | 2 / 3 | -4.7327 | -50.00 | 1 |
| research_loss_cooldown_24h | 2 / 3 | -4.7327 | -50.00 | 1 |
| research_loss_cooldown_48h | 2 / 3 | -4.7327 | -50.00 | 1 |
| research_evo_68570b89ba | 0 / 0 | 0.0000 | 0.00 | 1 |

## Validation and rollout

285 local unittest checks passed, including seven new tests for fresh temporal entries, observed TP4 cost accounting, stop and gap handling, duplicate scanner timestamps, no historical journal import, and conservative reporting. Automated CI runs the added tests. strategy_evidence is included in each optimizer report, including the early return for active temporal PAPER mode. This is a forward research deployment, not a profitability claim.
