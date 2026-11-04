# PAPER swing v1 — user-requested experiment, 2026-11-04

The main PAPER ledger has 600 closes, cumulative -1588.056238 SDA.
Its TP4 cohort has four closes, net -5.023165 SDA; these are distinct
from the independent research TP4 journal (three valid closes +0.886366 SDA).
RIDEX (Nov 4) reached only +1.2984% gross MFE, then closed via TP4 TRAIL
with -2.4131% gross move. Activating a trail before covering modeled costs
allows a small bounce to become a losing trailing exit.

New PAPER positions only:
- Investment 20 SDA, explicitly overriding the legacy engine's 50 SDA minimum.
- Raw reference stop -8%, take +16%, maximum hold 48 hours.
- Trail 6% from the observed peak, activated only at a +10% peak.
- First fresh observed price at/after a threshold is used; no fills at an
  unavailable stop price. Stale quotes never produce a simulated close.
- Existing positions without swing_v1 retain their TP4 exit rules.
- Swing cohort totals are separately reported, while the inclusive main
  PAPER total retains all old trades, invalid and unknown observations.

BUY preserves v9 admission and adds a slow-rebound fallback: current and
prior scores >=30, current quality >=30, >=2 hourly trades, hourly volume
>=250 SDA, hourly net flow >=100 SDA, 15m flow >=10 SDA, buy ratio >=1.3,
hourly move -12..10%, 15m move -0.5..8%, nonnegative impulse. NO phase
is allowed at these thresholds. Two independent quotes must still show
strictly increasing price <=5%, the new quote must follow the prior decision,
current quote age <=300 seconds, intervals <=900 seconds. The independent
quote continuity gate, price/flow sanity, MACD veto, loss circuit, cooldown,
max-open and max-new-entry limits remain enforced.

At a threshold-observed exit, with the actual engine's 1% fee and 0.1%
slippage per side, old 50 SDA / -2.5% TP4 stop implies -2.33393 SDA;
new 20 SDA / -8% stop implies -2.02040 SDA. New +16% take implies
+2.72211 SDA. Gaps can cause larger losses; these are modeled, not guaranteed.

Evidence limits: inspected all 600 ledger entries, 175622 stored transactions
for 164 token addresses, candidate outcomes and the latest 100-minute funnel.
The versioned market transaction history ends Oct 7 despite newer analysis/scanner
snapshots; the checkpoint intentionally excludes the runtime market_data blob. It cannot replay the recent TP4 positions after their original
exits. Historical replay of old entries, requiring <=30 minute quote gaps,
remained negative across the illustrative exit profiles; broader stops have
not established profitability. The new rules are an explicitly requested
forward PAPER hypothesis, not a selected profitable backtest or proven
SELL execution. Original research journal entries/exits and real trading
are unchanged.
