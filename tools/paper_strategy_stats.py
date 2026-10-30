"""Main PAPER strategy cohort; unknown quality is retained in financial totals."""
import math

def number(v):
 try:
  n=float(v);return n if math.isfinite(n) else None
 except (ValueError,TypeError):return None

def cohort(ledger):
 closed=[t for t in ledger.get('closed_trades',[]) if t.get('paper_strategy')=='temporal_recovery_tp4']
 opened=[t for t in (ledger.get('positions') or {}).values() if t.get('paper_strategy')=='temporal_recovery_tp4']
 vals=[number(t.get('closed_profit_sda')) for t in sorted(closed,key=lambda t:t.get('closed_at',''))]
 eq=peak=dd=0
 for v in vals:
  if v is None:continue
  eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
 valid=sum(t.get('paper_quote_quality_valid') is True for t in closed)
 invalid=sum(t.get('paper_quote_quality_valid') is False for t in closed)
 unknown=len(closed)-valid-invalid
 return dict(strategy='temporal_recovery_tp4',closed=len(closed),net_sda=round(eq,6),
             drawdown_sda=round(dd,6),wins=sum(v is not None and v>0 for v in vals),
             tokens=len({t.get('address') for t in closed}),open=len(opened),
             quality_valid=valid,quality_invalid=invalid,quality_unknown=unknown,
             accounting_unknown=sum(v is None for v in vals),
             unverified_sellability=len(closed),
             open_principal_at_risk_sda=round(sum((number(t.get('investment_sda')) or 0)*(number(t.get('remaining_fraction',1)) or 0) for t in opened),6),
             milestone_15_reached=valid>=15,includes_invalid_and_unknown_in_net=True)

def lines(s):
 return ['', '🧪 MAIN PAPER TP4 — since deployment',
         f"Closed {s['closed']} • wins {s['wins']} • tokens {s['tokens']} • open {s['open']}",
         f"Net {s['net_sda']:+.2f} SDA • closed drawdown {s['drawdown_sda']:.2f} SDA",
         f"Quote quality: valid {s['quality_valid']} / invalid {s['quality_invalid']} / unknown {s['quality_unknown']}",
         f"Open principal at risk: {s['open_principal_at_risk_sda']:.2f} SDA",
         'Net includes invalid/unknown observations. Simulated trades; SELL fills unverified.']
