# Autonomous SDA paper trading – forward learner

This system is **paper-only**. Its objective is to improve **net SDA accumulated**
after modeled trading friction while bounding loss and drawdown. No trading
strategy can guarantee a maximum profit. The optimization engine is a
deterministic, self-running model-selection agent, not an LLM autonomously
rewriting arbitrary source code.

## Closed loop

1. Long-running scanner writes contemporaneous V30 candidate snapshots.
2. `tools.forward_shadow_agent` consumes the latest snapshot with subsequent
   per-token transaction timestamps. It journals **separate** paper positions
   for each strategy. No wallet/private-key API is called.
3. Every six hours, the `Autonomous SDA Paper Agent` workflow evaluates
   forward-only closed virtual trades; chronological **training** chooses one
   challenger and **holdout** judges it. Unvalidated WATCH observations remain
   research-only and cannot be promoted.
4. If forward evidence has sufficient breadth, positive modeled **net SDA**
   and acceptable drawdown, `tools.forward_shadow_optimizer` updates
   `v30_agent_config.json` in **paper** mode. It does not alter exits or
   real-trading configuration.
5. Watchdog restarts an older scanner after its grace period to pick up the
   new Champion filters. A promoted Champion is rolled back automatically
   if enough subsequent **actual paper-trade** results breach limits.
6. Every scheduled optimizer run generates a Telegram PNG with status and
   per-strategy outcomes. Source-commit test runs suppress Telegram spam.

## Research policies

Six deployable parameter combinations compete on the current V30-supported
entry filters: score, quality, impulse, buy ratio and activity.
Two extra WATCH variants investigate missed opportunities but are forbidden
from automatic deployment because the live paper entry engine does not
support WATCH entries.

## Safeguards and limitations

- Shadow entries are based only on market snapshots visible at entry time.
  Closing a virtual trade requires a later transaction timestamp.
- Modeled execution friction: 1.1% entry and 1.1% exit (fees + slippage).
  The shadow exit approximation is stop −2.5%, take +8%, trailing −3.5%
  or time 4 hours, and **is not identical to execution-engine fills**.
- Quote gaps above 30 minutes, implausible jumps and stale-timeout writeoffs
  are excluded from positive learning evidence. At least **90%** of a
  challenger's journal must be valid observed-quote outcomes.
- Candidate selection requires >=30 valid chronological training closes
  across >=6 tokens, with positive after-cost P/L.
- The single preselected candidate needs >=15 chronological holdout closes
  across >=6 tokens, >=10 SDA holdout modeled profit, positive profit factor
  >=1.15, holdout win rate >=35%, max drawdown <=50 SDA and an improvement
  over the observed baseline.
- Promotion changes **paper** config only. It requires new evidence before
  further promotion. After >=8 subsequent actual paper closes, a loss below
  −25 SDA or >50 SDA drawdown triggers rollback; >=15 closes and
  negative accumulated SDA also trigger rollback.
- **No promotion can be inferred from the existing historical closed-trade
  ledger alone**, because it contains only fills chosen by prior strategies.
  Legacy retrospective V30 optimizer is research-only and cannot overwrite
  the active config.
- Simulated quotes are NOT executable bids or guaranteed liquidity.
  The only trustworthy profitability goal is observed future paper net P/L;
  this framework is not evidence that real trading would be profitable.

## Operational status

- `WAITING_FOR_FRESH_SCANNER`: current worker has not yet checkpointed
  new forward-shadow state, or data is older than 30 minutes.
- `COLLECTING_FORWARD_TRADES`: running, but too few completed research trades.
- `NO_PROFITABLE_TRAIN_CHALLENGER`: no positive train signal.
- `HOLDOUT_REJECTED`: candidate failed untouched forward holdout.
- `PROMOTED_PAPER`: Champion paper config advanced one generation.
- `MONITORING_PAPER_PROMOTION`: validation of actual subsequent paper trades.
- `ROLLED_BACK_PAPER`: autonomous risk guard restored prior paper settings.

Sources: `strategy_lab/forward_shadow_state.json`,
`strategy_lab/autonomous_agent_report.json`,
`v30_agent_config.json`, `paper_stats.json`.
The scanner state checkpoint is authoritative. Do not reset it to `{}`
or populate historic forward results from present-day quotes.

Run unit/integration checks using the GitHub Actions workflow
`Autonomous SDA Paper Agent`; the scheduled job runs at minute 42 every
six hours UTC.
