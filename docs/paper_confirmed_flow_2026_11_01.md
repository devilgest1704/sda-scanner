# PAPER TP4 confirmed continuation

TP4 previously selected strict stale-to-fresh recovery only. Existing evaluate_paper already supports independently observed fresh continuation but was bypassed in TP4. Route TP4 through it, retaining the 8% 15m cap and positive current flow guard. Preserve the actual entry lane and mark tp4_confirmed_flow_v2 for attribution.

Continuation needs a prior fresh IGNITION/CONFIRMATION, a strictly newer quote after the prior decision, positive price follow-through capped at 5%, current age <=180s, score >=70, quality >=62, impulse >=6, 1h move 1..10%, 15m move >=1.5%, volume >=700, 1h flow >=220, 15m flow >=60, >=8 trades and buy ratio >=1.4. For 8..11 trades the existing stronger requirements and outer activity gate still apply. Missing evidence fails closed.

Scope: new main PAPER entries only; quote continuity, cooldown, MACD veto, loss budget, stake, exits and independent SHADOW stay unchanged. This is an explicitly requested experimental frequency expansion, not a validated profitable filter. Historical main continuation cohort: 7 closes, -18.029352 SDA under older exits. It cannot establish TP4 outcomes; no favorable profitability claim is made. Latest diagnostic at 2026-11-01T12:37Z has no ready signal even before expansion, so no immediate BUY is promised.
