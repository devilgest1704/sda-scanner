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

## REAL BUY market-source outages

Both collectors use the PinetSwap Supabase `public.token_transactions` feed.
Transient failures use bounded retries with jitter. A long `Retry-After` is
honored across scans without blocking the scan loop. Failure diagnostics store
the response status, server, Ray ID and safe proxy/error codes, not raw bodies
or credentials. An HTTP success is not sufficient: the market collector must
see a valid recent trade before publishing healthy data. Each scan resets its
BUY availability flag; existing wallet, signal and capacity gates still apply.

### Independent fallback proposal (disabled)

Run a separate read-only indexer using confirmed PinetSwap pool `Swap` logs
from an independently operated Sidra RPC, with `https://node.sidrachain.com`
as an initial validation endpoint. Reuse the ABI decoding in the liquidity
scanner, discover and verify the covered pools and token decimals, and use
receipts and block timestamps to reconstruct `token_transactions` rows:
hash, token, direction, participants, SDA price, SDA volume and timestamp.
Do not treat router-level sender/recipient as user wallet addresses without
verifying receipt semantics. Preserve unique log identity, reconcile reorgs,
and handle duplicate, multi-pool, multi-hop and non-WSDA routes explicitly.

Before enabling BUY fallback, compare both feeds in shadow mode over an
overlapping production period. Verify coverage and lag against the confirmed
chain tip; compare per-swap values and both 1h/4h flow and whale aggregates;
check that admission decisions match. Test provider limits, gaps and outage
recovery. Unverified coverage, excessive lag, ambiguous mappings or mismatch
must keep REAL BUY closed. A SELL reference quote is not a transaction feed.
