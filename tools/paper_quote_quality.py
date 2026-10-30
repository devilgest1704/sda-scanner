"""PAPER-only observed quote continuity. No SELL/fill verification."""
import json, math
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
WINDOW_SECONDS=900
MAX_GAP_SECONDS=600
MIN_QUOTES=3
MAX_AGE_SECONDS=300

def stamp(v):
 try:
  t=datetime.fromisoformat(str(v).replace('Z','+00:00'))
  return t.astimezone(timezone.utc) if t.tzinfo else None
 except (ValueError,TypeError):return None

def evaluate(transactions, at, quote_at):
 result=dict(ready=False,reason='missing_quote_history',window_seconds=WINDOW_SECONDS,
             max_allowed_gap_seconds=MAX_GAP_SECONDS,minimum_distinct_quotes=MIN_QUOTES,
             observed_quotes=0,max_gap_seconds=None,sellability_verified=False)
 quote=stamp(quote_at)
 if not isinstance(at,datetime) or at.tzinfo is None or quote is None or not 0<=(at-quote).total_seconds()<=MAX_AGE_SECONDS:
  return {**result,'reason':'stale_or_invalid_current_quote'}
 times=set()
 for tx in transactions if isinstance(transactions,list) else []:
  if not isinstance(tx,dict):continue
  t=stamp(tx.get('timestamp'))
  try:price=float(tx.get('price'))
  except (ValueError,TypeError):continue
  if math.isfinite(price) and price>0 and t and at-timedelta(seconds=WINDOW_SECONDS)<=t<=at:times.add(t)
 ordered=sorted(times);result['observed_quotes']=len(ordered)
 if not ordered:return result
 if ordered[-1]!=quote:return {**result,'reason':'current_quote_history_mismatch'}
 boundaries=[at-timedelta(seconds=WINDOW_SECONDS),*ordered,at]
 result['max_gap_seconds']=round(max((b-a).total_seconds() for a,b in zip(boundaries,boundaries[1:])),3)
 if len(ordered)<MIN_QUOTES:return {**result,'reason':'too_few_distinct_quotes'}
 if result['max_gap_seconds']>MAX_GAP_SECONDS:return {**result,'reason':'quote_history_gap'}
 return {**result,'ready':True,'reason':'OBSERVED_CONTINUITY_PASSED'}

@lru_cache(maxsize=1)
def market_snapshot():
 try:
  data=json.loads(Path('market_data.json').read_text())
  return data if isinstance(data,dict) else {}
 except (OSError,ValueError):return {}

def entry_gate(address, quote_at, at=None):
 at=at or datetime.now(timezone.utc);data=market_snapshot();source=stamp(data.get('updated_at'))
 if source is None or not 0<=(at-source).total_seconds()<=MAX_AGE_SECONDS:
  return dict(ready=False,reason='stale_or_missing_market_snapshot',sellability_verified=False)
 tokens=data.get('tokens')
 token=tokens.get(str(address).lower()) if isinstance(tokens,dict) else {}
 token=token if isinstance(token,dict) else {}
 return evaluate(token.get('transactions'),at,quote_at)
