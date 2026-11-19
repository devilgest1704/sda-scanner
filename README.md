SDA Scanner

<!-- vercel production deploy trigger 2026-09-23 08:xx; bounded CLI timeout -->

<!-- V30 state bridge: 2026-09-23 -->

## REAL SELL V2: stagnant profit

`MAX_HOLD_STAGNANT_PROFIT` runs after loss limit, hard TP, partial TP and
trailing stop. Age alone never triggers it. It requires a positive mark,
72 hours of age, and less than 1 percentage point of improvement over the
last 24 hours. At least 75% of the window must be covered by fresh samples;
gaps over 30 minutes do not count. The reference price and scanner trend
must be at most 300 seconds old. Missing required trend data, bullish
momentum or recovery, an active SELL intent, partial TP and armed trailing
all prevent a MAX HOLD exit.

The REAL position stores time-bounded price/PnL observations and the latest
64 shadow evaluations. No PAPER state is used. There is no history backfill:
existing positions start with no eligible history until the worker collects it.
The full-quantity router simulation already includes the pool fee and price
impact. The NET check subtracts the remaining basis and a gas budget for the
swap and unwrap. `minOut` retains the existing slippage protection and is
raised above break-even when needed. Missing allowance blocks the rule;
it does not send an approval just to obtain a quote.

For read-only diagnostics on Linux, run from a current state checkout:

```bash
REAL_TRADING_ENABLED=false REAL_TRADING_DRY_RUN=true python -m tools.real_max_hold_preflight
```

Each JSON line reports one REAL position's source, age, gross and estimated
NET exit PnL, history coverage, trend and decision blockers. A null value means
the corresponding data could not be established. This command never writes
state, signs or broadcasts transactions, or contacts Telegram.
