# PAPER capacity and net valuation — 2026-11-05

User requested up to ten simultaneous PAPER positions. SDA_PAPER_TP4=1 now
sets the active strategy and patched engine cap to ten. Default non-TP4
capacity remains four. One new BUY per scanner cycle remains enforced.
New swing stake remains 20 SDA: nominal open principal can increase from
80 to 200 SDA (entry fees add to deployed cost). Existing exits, MACD veto,
cooldowns and the 24h loss circuit remain unchanged. Increasing concurrent
positions increases aggregate exposure, including correlated token losses.
No real-trading limits or orders are changed.

paper_stats.json previously valued open positions as investment times the
raw move from the slippage-adjusted entry, omitting entry fee and exit costs.
The JSON generator now matches actual PAPER close() and the existing canonical
Telegram renderer: quantity * current price * (1-exit slippage) * (1-exit fee)
minus principal * (1+entry fee).
Entry price already contains entry slippage, so it is not charged again.
Canonical realized records remain unchanged. Open ROI uses allocated entry
cost as the denominator. The report labels stale/unknown open quote counts;
last observed marks are not verified executable SELL fills.

At the 08:00 Prague snapshot, four open swing positions had gross marked
P/L +7.806950 SDA but net model P/L +6.041952 SDA. Including the first swing
stop (-2.093913 SDA), the new cohort's marked net was +3.948038 SDA. This
includes unrealized, stale-quote marks and does not establish profitability.

Validation: full suite 365 tests passes. New regressions check import-time
PAPER/non-PAPER capacity, actual patched engine limits, one-BUY-per-cycle,
open P/L against actual close() for wins/losses/partial fractions/custom fees,
and stale/unknown quote labeling without inventing closes.
