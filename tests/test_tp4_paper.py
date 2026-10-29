import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import v30_pump_hunter as v

class TP4PaperTests(unittest.TestCase):
    def run_exit(self, price, minutes=2, peak=100, age=30, tagged=True):
        now=datetime.now(timezone.utc)
        def create(a,an,s,meta,liq,investment=None):
            return dict(address=a, opened_at=(now-timedelta(minutes=minutes)).isoformat(),
                        entry_price=100.1,reference_price=100,investment_sda=investment)
        def close(book,a,price,reason):
            row={**book['positions'].pop(a),'close_reason':reason,'close_price':price}
            book['closed_trades'].append(row.copy());return row
        d=dict(pump_score=75,pump_quality=65,pump_change=10,pump_phase='IGNITION',
               m1h=2,m15=1,m4h=1,net_1h=250,volume_1h=800,trades_1h=15,data_age_sec=age)
        e=SimpleNamespace(create=create,close=close,main=lambda:None)
        with patch.object(v,'PAPER_TP4',True),patch.object(v,'decision',return_value=d),patch.object(v,'load_state',return_value={}),patch.object(v,'save_state'):
            v.patch(SimpleNamespace(),e)
            pos=e.create('x',{'price_in_sda':100},d,{}, {},100)
            self.assertEqual(pos['investment_sda'],50)
            pos['pump_peak_price']=peak
            if not tagged:pos.pop('paper_strategy')
            book=dict(positions={'x':pos},closed_trades=[])
            e._auto_exit(book,{'x':{'analysis':{'price_in_sda':price}}},{})
        return book
    def test_take_uses_raw_reference_not_slippage_entry(self):
        self.assertEqual(self.run_exit(104)['closed_trades'][0]['close_reason'],'TP4 TAKE')
    def test_stop(self):
        self.assertEqual(self.run_exit(97.4)['closed_trades'][0]['close_reason'],'TP4 STOP')
    def test_time(self):
        self.assertEqual(self.run_exit(101,minutes=241)['closed_trades'][0]['close_reason'],'TP4 TIME')
    def test_trail_after_15_minutes(self):
        self.assertEqual(self.run_exit(100,minutes=16,peak=103.9)['closed_trades'][0]['close_reason'],'TP4 TRAIL')
        self.assertFalse(self.run_exit(100,minutes=14,peak=103.9)['closed_trades'])
    def test_stale_quote_never_fills(self):
        self.assertFalse(self.run_exit(110,age=301)['closed_trades'])
    def test_legacy_position_keeps_legacy_exit(self):
        self.assertFalse(self.run_exit(104,tagged=False)['closed_trades'])
