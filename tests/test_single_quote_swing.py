"""No second quote/history entry brake; current-data and risk gates remain."""
import unittest
from unittest.mock import patch
from datetime import timedelta
from tools.temporal_recovery_buy import evaluate_tp4,evaluate_tp4_v10
from tools.paper_quote_quality import entry_gate
from tests.test_paper_confirmed_continuation import NOW,ADDRESS,candidate,ActivePaperEntryTests
import v30_pump_hunter as v

class SingleQuoteSwingTests(unittest.TestCase):
    def sample(self,**kw):
        return dict(candidate(phase='NO',score=30,quality=30,impulse=0,trades_1h=2,
                              volume_1h=250,flow_1h=100,flow_15m=10,buy_ratio=1.3,m1h=-10,m15=0),**kw)
    def test_single_quote_can_enter_without_prior_or_price_followthrough(self):
        c=self.sample()
        self.assertFalse(evaluate_tp4_v10(ADDRESS,c,at=NOW,previous={})['ready'])
        r=evaluate_tp4(ADDRESS,c,at=NOW,previous={})
        self.assertTrue(r['ready']);self.assertEqual(r['lane'],'swing_single_quote')
        self.assertFalse(r['sellability_verified']);self.assertFalse(r['continuity_verified'])
    def test_current_age_and_all_strength_boundaries_remain(self):
        for kw in [dict(data_age_sec=301),dict(data_age_sec=-1),dict(data_age_sec=None),
                   dict(quote_at=(NOW+timedelta(seconds=1)).isoformat()),dict(price_sda=0),
                   dict(price_sda=float('nan')),dict(score=29),dict(quality=29),
                   dict(volume_1h=249),dict(flow_1h=99),dict(flow_15m=9),
                   dict(trades_1h=1),dict(buy_ratio=1.29),dict(m15=9),dict(m1h=11)]:
            with self.subTest(kw=kw):
                self.assertFalse(evaluate_tp4(ADDRESS,self.sample(**kw),at=NOW,previous={})['ready'])
    def test_history_not_read_in_single_quote_mode_and_default_keeps_it(self):
        qt=self.sample()['quote_at']
        with patch('tools.paper_quote_quality.market_snapshot',return_value={}) as history:
            r=entry_gate(ADDRESS,qt,at=NOW,require_continuity=False)
            history.assert_not_called();self.assertTrue(r['ready']);self.assertFalse(r['continuity_verified'])
            self.assertFalse(entry_gate(ADDRESS,qt,at=NOW)['ready'])
        self.assertFalse(entry_gate(ADDRESS,(NOW-timedelta(seconds=301)).isoformat(),at=NOW,require_continuity=False)['ready'])
    def test_actual_outer_gate_admits_one_quote_but_not_risk_blocks(self):
        result=evaluate_tp4(ADDRESS,self.sample(),at=NOW,previous={})
        for kw in [{},{'loss':True},{'macd':True},{'cooldown':True}]:
            with patch.object(v,'PAPER_TP4',True),patch.object(v,'strict_temporal_signal',return_value=result),patch.object(v,'quote_quality_gate',side_effect=lambda a,q,**kwargs:entry_gate(a,q,at=NOW,**kwargs)) as gate:
                d=ActivePaperEntryTests().decide(trades=2,**kw)
                self.assertEqual(d['eligible_for_buy'],not bool(kw),d)
                self.assertFalse(gate.call_args.kwargs['require_continuity'])
    def test_single_quote_close_retains_unknown_continuity(self):
        from tests.test_paper_swing import SwingExitTests
        # Entry metadata is passed through actual create/close, not invented.
        original=v.patch
        def attach(main,e):
            original(main,e);create=e.create
            def one(*a,**kw):
                pos=create(*a,**kw)
                pos['paper_entry_quote_quality']=dict(ready=True,continuity_verified=False,sellability_verified=False)
                return pos
            e.create=one
        with patch.object(v,'patch',side_effect=attach):
            t=SwingExitTests().run_exit(152)['closed_trades'][0]
        self.assertIsNone(t['paper_quote_quality_valid'])
        self.assertFalse(t['paper_sellability_verified'])
