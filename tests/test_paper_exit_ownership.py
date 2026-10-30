import unittest
from unittest.mock import patch
from contextlib import ExitStack
import engine_legacy as e

class ExitOwnershipTests(unittest.TestCase):
 def run_case(self,price,managed):
  pos=dict(address='x',label='X',entry_price=1,investment_sda=50,remaining_fraction=1,tp1=1.05,tp2=1.1,sl=.975,opened_at='2026-10-30T01:00:00Z')
  if managed:pos['paper_strategy']='temporal_recovery_tp4'
  book=dict(positions={'x':pos},closed_trades=[])
  market={'tokens':{'x':{'analysis':{'price_in_sda':price}}}}
  def load(path,default):return book if path==e.POSITIONS_FILE else market if path==e.MARKET_FILE else default
  with ExitStack() as st:
   for name in ['send','save','wallet_message','portfolio_message','portfolio_recommendations','portfolio_history','wallet_snapshot']:
    st.enter_context(patch.object(e,name,return_value={}))
   st.enter_context(patch.object(e,'load',side_effect=load))
   callback=st.enter_context(patch.object(e,'_auto_exit',return_value=[]))
   e.main();callback.assert_called_once()
  return book
 def test_managed_stale_stop_is_not_closed_by_legacy(self):
  b=self.run_case(.9,True);self.assertIn('x',b['positions']);self.assertFalse(b['closed_trades'])
 def test_managed_partial_take_is_not_silently_applied(self):
  b=self.run_case(1.08,True);self.assertEqual(b['positions']['x']['remaining_fraction'],1);self.assertFalse(b['positions']['x'].get('tp1_hit'))
 def test_legacy_position_still_exits(self):
  self.assertEqual(self.run_case(.9,False)['closed_trades'][0]['close_reason'],'SL')
