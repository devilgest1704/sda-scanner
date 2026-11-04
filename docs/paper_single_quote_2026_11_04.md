# PAPER single-quote swing v11

User explicitly requested removal of the quote entry brake.

The active PAPER entry gate no longer requires a second quote, a prior frozen
signal, positive price follow-through, a minimum quote count, or a historical
quote continuity window. Default quote-gate callers and independent SHADOW
research keep their original checks.

A fallback admits the existing swing strength profile from the CURRENT quote:
score and quality >=30, nonnegative impulse, >=2 hourly trades, volume >=250
SDA, hourly net flow >=100 SDA, 15m flow >=10 SDA, ratio >=1.3, hourly move
-12..10%, 15m move -0.5..8%. Missing/nonfinite/invalid input fails closed.
Current quote and declared data age must both be <=300 seconds and agree;
future quotes are invalid. Existing outer price/flow sanity, anti-chase,
MACD veto, loss circuit, cooldown and position limits still apply.

The entry gate records continuity_verified=false and sellability_verified=false.
Closed-trade continuity remains unknown for these entries; a passed current
quote check does not make them verified-history trades. Swing size 20 SDA,
SL -8%, TP +16%, trail 6% activated at +10%, and 48h hold remain unchanged.
Legacy positions keep their frozen exit ownership. Real trading is unchanged.

This removes confirmation evidence and can increase false entries. It is a
user-requested forward PAPER experiment, not established profitability or
proof of executable SELL liquidity. No trades are forced when current filters
fail.
