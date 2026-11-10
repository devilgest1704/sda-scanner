"""PAPER swing admission and real engine exit/cost integration."""
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import engine  # production engine (paper_stats_patch moves partial rows to partial_exits)
import v30_pump_hunter as v
from tools.temporal_recovery_buy import evaluate_tp4_v10 as evaluate_tp4,evaluate_tp4_legacy,evaluate_paper
from tests.test_paper_confirmed_continuation import NOW,ADDRESS,prior,candidate,ActivePaperEntryTests

class SwingAdmissionTests(unittest.TestCase):
    def sample(self,**kw):
        c=candidate(phase='NO',score=30,quality=30,impulse=0,trades_1h=2,
                    volume_1h=250,flow_1h=100,flow_15m=10,buy_ratio=1.3,m1h=-10,m15=0)
        return dict(c,**kw)
    def test_slow_rebound_needs_new_price_not_score_acceleration(self):
        p=prior(phase='NO',score=30,impulse=0);c=self.sample()
        r=evaluate_tp4(ADDRESS,c,at=NOW,previous=p)
        self.assertTrue(r['ready'],r);self.assertEqual(r['entry_variant'],'swing_rebound')
        self.assertFalse(evaluate_tp4_legacy(ADDRESS,c,at=NOW,previous=p)['ready'])
        self.assertFalse(evaluate_paper(ADDRESS,c,at=NOW,previous=p)['ready'])
    def test_boundaries_and_missing_or_stale_quote_fail_closed(self):
        p=prior(phase='NO',score=30,impulse=0)
        for kw in [dict(score=29),dict(quality=29),dict(trades_1h=1),dict(volume_1h=249),
                   dict(flow_1h=99),dict(flow_15m=9),dict(buy_ratio=1.29),dict(m1h=-12.1),
                   dict(m1h=10.1),dict(m15=8.1),dict(m15=-.51),dict(price_sda=.98),
                   dict(price_sda=1.04),dict(data_age_sec=301),dict(quote_at=p['quote_at'])]:
            with self.subTest(kw=kw):self.assertFalse(evaluate_tp4(ADDRESS,self.sample(**kw),at=NOW,previous=p)['ready'])
        self.assertFalse(evaluate_tp4(ADDRESS,self.sample(),at=NOW,previous={})['ready'])
    def test_outer_risk_controls_and_quote_gate_still_apply(self):
        r=evaluate_tp4(ADDRESS,self.sample(),at=NOW,previous=prior(phase='NO',score=30,impulse=0))
        for kw in [{},{'loss':True},{'macd':True},{'cooldown':True}]:
            for quote_ready in [True,False]:
                with patch.object(v,'PAPER_TP4',True),patch.object(v,'strict_temporal_signal',return_value=r),patch.object(v,'quote_quality_gate',return_value=dict(ready=quote_ready,reason='test')):
                    d=ActivePaperEntryTests().decide(trades=2,**kw)
                    self.assertEqual(d['eligible_for_buy'],quote_ready and not bool(kw))

class SwingExitTests(unittest.TestCase):
    def run_exit(self,price,*,peak=100,hours=1,quote_age=20,legacy=False):
        d=dict(confidence=40,pump_score=40,pump_quality=40,pump_change=0,pump_phase='NO',
               m1h=0,m15=0,m4h=0,net_1h=100,volume_1h=250,trades_1h=2,data_age_sec=quote_age)
        e=SimpleNamespace(create=engine.create,close=engine.close,main=lambda:None)
        with patch.object(v,'PAPER_TP4',True),patch.object(v,'decision',return_value=d),patch.object(v,'load_state',return_value={}),patch.object(v,'save_state'):
            v.patch(SimpleNamespace(),e)
            pos=e.create('x',{'price_in_sda':100},d,{}, {},100)
            self.assertEqual(pos['investment_sda'],20) # real engine clamps to >=50 unless overridden
            self.assertAlmostEqual(pos['sl'],100.1*1.01*.5)
            self.assertAlmostEqual(pos['tp1'],100.1*1.01*1.25)
            self.assertEqual(pos['paper_exit_profile'],'swing_v2')
            pos.update(opened_at=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(),pump_peak_price=peak)
            if legacy:pos.pop('paper_exit_profile')
            book=dict(positions={'x':pos},closed_trades=[])
            qt=(datetime.now(timezone.utc)-timedelta(seconds=quote_age)).isoformat()
            e._auto_exit(book,{'x':{'analysis':{'price_in_sda':price,'last_transaction':qt}}},{})
            return book
    def test_no_premature_stop_take_or_trail(self):
        for price,peak in [(97,100),(104,104),(98,101.3),(105,109.9),(51,100),(126,130)]:
            with self.subTest(price=price):self.assertFalse(self.run_exit(price,peak=peak)['closed_trades'])
    def test_stop_take_and_costs(self):
        for price,reason in [(50,'SWING V2 STOP -50'),(152,'SWING V2 TP +50')]:
            t=self.run_exit(price)['closed_trades'][0]
            self.assertEqual(t['close_reason'],reason)
            self.assertAlmostEqual(t['closed_profit_sda'],20*(price/100*.999*.99/1.001-1.01))
            self.assertFalse(t['paper_sellability_verified'])
    def test_first_profit_is_a_partial_not_a_full_exit(self):
        book=self.run_exit(127)
        t=book['partial_exits'][0]
        self.assertFalse(book['closed_trades'])
        self.assertEqual(t['close_reason'],'SWING V2 PARTIAL +25');self.assertEqual(t['status'],'PARTIAL CLOSED')
        self.assertAlmostEqual(book['positions']['x']['remaining_fraction'],.8)
    def test_no_time_exit_and_stale_data(self):
        self.assertFalse(self.run_exit(101,hours=47.9)['closed_trades'])
        self.assertFalse(self.run_exit(101,hours=500)['closed_trades'])
        self.assertFalse(self.run_exit(40,hours=49,quote_age=301)['closed_trades'])
    def test_existing_tp4_keeps_old_exit_rules(self):
        self.assertEqual(self.run_exit(104,legacy=True)['closed_trades'][0]['close_reason'],'TP4 TAKE')

class SwingReportingTests(unittest.TestCase):
    def test_legacy_and_swing_financial_totals_remain_distinguishable(self):
        from tools.paper_strategy_stats import cohort,lines
        ledger=dict(closed_trades=[dict(paper_strategy='temporal_recovery_tp4',address='a',closed_profit_sda=-5),
                                  dict(paper_strategy='temporal_recovery_tp4',paper_exit_profile='swing_v1',address='b',closed_profit_sda=2)],
                    positions={'c':dict(paper_strategy='temporal_recovery_tp4',paper_exit_profile='swing_v1',investment_sda=20)})
        s=cohort(ledger)
        self.assertEqual(s['net_sda'],-3)
        self.assertEqual(s['swing_v1']['net_sda'],2)
        self.assertEqual(s['swing_v1']['open_principal_at_risk_sda'],20)
        self.assertIn('Swing v1:',lines(s)[-2])
        self.assertIn('Swing SELL V2:',lines(s)[-1])
