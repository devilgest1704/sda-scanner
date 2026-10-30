"""Deterministic train-only exit search; truncated paths never imply future fills."""
import json,itertools,hashlib
from pathlib import Path
from collections import Counter
from tools.v30_exit_parity import timestamp,finite
from tools.buy_entry_research import research

def stats(rows):
 vals=[r['net'] for r in sorted(rows,key=lambda r:r['closed'])];eq=peak=dd=0
 for x in vals: eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
 gains=sum(x for x in vals if x>0);loss=-sum(x for x in vals if x<0)
 return dict(count=len(vals),tokens=len({r['token'] for r in rows}),net=round(sum(vals),6),drawdown=round(dd,6),wins=sum(x>0 for x in vals),profit_factor=gains/loss if loss else None)

def replay(t,rule):
 opened=timestamp(t.get('opened_at'));entry=finite(t.get('reference_price'));inv=finite(t.get('investment_sda'));samples=t.get('pump_price_samples')
 if not opened or not entry or entry<=0 or not inv or not samples:return None,'missing_path'
 take,stop,trail,hours=rule;previous=opened;peak=entry
 for s in samples:
  at=timestamp(s.get('at'));quote=timestamp(s.get('quote_at'));price=finite(s.get('price_sda'));age=finite(s.get('quote_age_seconds'))
  if not at or not quote or not price or price<=0 or age is None:return None,'missing_quote_evidence'
  if at<previous or quote>at or not 0<=age<=300 or abs((at-quote).total_seconds()-age)>90:return None,'unsafe_quote_time'
  if (at-previous).total_seconds()>1200:return None,'observation_gap_over_20m'
  peak=max(peak,price);duration=(at-opened).total_seconds()/3600;move=price/entry-1
  reason='STOP' if move<=-stop else 'TAKE' if move>=take else 'TRAIL' if duration>=.25 and price/peak-1<=-trail else 'TIME' if duration>=hours else None
  if reason:
   # Main PAPER cost formula, normalized to 50 SDA principal.
   return dict(at=opened.isoformat(),closed=at.isoformat(),token=t['address'],net=50*((price/entry)*.999*.99/1.001-1.01),reason=reason),'closed'
  previous=at
 return None,'right_censored_at_incumbent_exit'

def main():
 paths=['positions.json','strategy_lab/closed_trades.json','strategy_lab/candidate_outcomes.json','strategy_lab/forward_shadow_state.json']
 raw={p:Path(p).read_bytes() for p in paths};data={p:json.loads(v) for p,v in raw.items()};ledger=data[paths[0]];archive=data[paths[1]]
 # Archive is audit context only; never add missing historical rows to current P/L.
 trades=sorted(ledger['closed_trades'],key=lambda t:t.get('opened_at',''));replay_trades=[t for t in trades if any(s.get('quote_at') for s in t.get('pump_price_samples',[]))];a,b=int(.6*len(replay_trades)),int(.8*len(replay_trades));parts=[replay_trades[:a],replay_trades[a:b],replay_trades[b:]]
 bounds=[min(t['opened_at'] for t in p) for p in parts[1:]]
 parts[0]=[t for t in parts[0] if t['closed_at']<bounds[0]];parts[1]=[t for t in parts[1] if t['closed_at']<bounds[1]]
 rules=list(itertools.product([.03,.04,.06,.08,.12],[.015,.025,.04],[.025,.035,.05],[1,2,4]))
 def assess(rows,rule):
  results=[replay(t,rule) for t in rows];closed=[r for r,_ in results if r];s=stats(closed);s['exclusions']=dict(Counter(reason for r,reason in results if r is None));s['coverage']=len(closed)/len(rows) if rows else 0;return s
 trials=[(rule,assess(parts[0],rule)) for rule in rules]
 eligible=[(r,s) for r,s in trials if s['count']>=30 and s['tokens']>=6 and s['coverage']>=.8 and s['net']>0 and (s['profit_factor'] or 0)>=1.15]
 best=max(eligible,key=lambda x:x[1]['net']-.25*x[1]['drawdown']) if eligible else None
 result=dict(source_hashes={p:hashlib.sha256(v).hexdigest() for p,v in raw.items()},current_closed=len(trades),current_net=round(sum(t.get('closed_profit_sda',0) for t in trades),6),current_open=len(ledger['positions']),audit_archive_rows=len(archive),audit_archive_net=sum(t.get('closed_profit_sda',0) for t in archive),current_exit_reasons=dict(Counter(t.get('close_reason') for t in trades)),price_paths=sum(bool(t.get('pump_price_samples')) for t in trades),quote_evidence_paths=sum(any(s.get('quote_at') for s in t.get('pump_price_samples',[])) for t in trades),buy_search=research(data[paths[2]]['observations']),exit_search=dict(tested=len(rules),split_sizes=list(map(len,parts)),all_paths_tp4=assess(trades,(.04,.025,.035,4)),qualifying_train=len(eligible),train_best_descriptive=max(trials,key=lambda x:x[1]['net']),selected=best,holdout_opened=bool(best)),can_promote=False,limitations=['Historical executed-cohort selection bias','No extrapolation beyond incumbent exit','Excluded/censored paths cannot be silently called profitable','Observed quotes are not executable fills'])
 if best:result['exit_search']['validation']=assess(parts[1],best[0]);result['exit_search']['test']=assess(parts[2],best[0])
 output=Path('docs/buy_sell_evidence_2026_10_30.json');output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
if __name__=='__main__':main()
