"""Runtime PAPER capacity and open valuation must match actual close economics."""
import copy,json,os,subprocess,sys,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from datetime import datetime,timezone
import paper_stats as stats
import engine_legacy as engine

class CapacityTests(unittest.TestCase):
    def test_runtime_cap_is_ten_only_for_tp4_and_one_buy_per_cycle(self):
        root=str(Path(__file__).resolve().parents[1])
        for enabled,limit in [('1',10),('0',4)]:
            env={**os.environ,'SDA_PAPER_TP4':enabled,'SDA_PAPER_BUY_MODE':'temporal_recovery'}
            code=f'''from types import SimpleNamespace
import v30_pump_hunter as v
assert v.MAX_OPEN == {limit}
e=SimpleNamespace()
v.patch(SimpleNamespace(),e)
assert e.MAX_OPEN_POSITIONS == {limit}
assert e.MAX_NEW_BUYS_PER_RUN == 1
assert max(0,e.MAX_OPEN_POSITIONS-{limit}) == 0
assert max(0,e.MAX_OPEN_POSITIONS-4) == {limit}-4
'''
            subprocess.run([sys.executable,'-c',code],cwd=root,env=env,check=True,capture_output=True)

class NetStatisticsTests(unittest.TestCase):
    def test_open_mark_matches_close_for_profit_loss_partial_and_nondefault_costs(self):
        for price,fraction,fee,slip in [(110,1,.01,.001),(90,1,.01,.001),(110,.5,.02,.003)]:
            with self.subTest(price=price,fraction=fraction,fee=fee):
                pos=dict(address='x',entry_price=100.1,investment_sda=20,remaining_fraction=fraction)
                ledger=dict(positions={'x':pos},closed_trades=[dict(closed_profit_sda=-2,investment_sda=20,closed_fraction=1)])
                market={'tokens':{'x':{'analysis':dict(price_in_sda=price,last_transaction=datetime.now(timezone.utc).isoformat())}}}
                with TemporaryDirectory() as folder,patch.object(engine,'FEE_RATE',fee),patch.object(engine,'SLIPPAGE_RATE',slip):
                    target=Path(folder)/'stats.json'
                    with patch.object(stats,'load',side_effect=lambda p,d:ledger if p==stats.POSITIONS_FILE else market),patch.object(stats,'STATS_FILE',str(target)),patch.object(stats,'send'):
                        stats.main()
                    result=json.loads(target.read_text())
                    book=copy.deepcopy(ledger);closed=engine.close(book,'x',price,'TEST')
                    self.assertAlmostEqual(result['open_pnl_sda'],closed['closed_profit_sda'])
                    self.assertAlmostEqual(result['cumulative_pnl_sda'],-2+closed['closed_profit_sda'])
                    self.assertEqual(result['realized_pnl_sda'],-2)
                    self.assertEqual(result['open_pnl_stale_quotes'],0)
                    self.assertTrue(result['open_pnl_after_model_costs'])
    def test_unknown_quote_is_labeled_without_inventing_a_fill(self):
        ledger=dict(positions={'x':dict(entry_price=1,investment_sda=20)},closed_trades=[])
        market={'tokens':{'x':{'analysis':dict(price_in_sda=1.1)}}}
        with TemporaryDirectory() as folder:
            target=Path(folder)/'stats.json'
            with patch.object(stats,'load',side_effect=lambda p,d:ledger if p==stats.POSITIONS_FILE else market),patch.object(stats,'STATS_FILE',str(target)),patch.object(stats,'send'):
                stats.main()
            result=json.loads(target.read_text())
            self.assertEqual(result['open_pnl_stale_quotes'],1)
            self.assertEqual(result['closed_trades'],0)
