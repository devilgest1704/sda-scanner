import json
import tempfile
import unittest
from pathlib import Path
from tools.strategy_lab import analyze
from tools.export_paper_trades import export

class StrategyLabTests(unittest.TestCase):
    def setUp(self):
        self.stats={"cumulative_pnl_sda":-1270.14,"closed_trades":468,"win_rate_pct":18.16}
        self.execution={"paper_only":True,"events":[{"event":"READY"},{"event":"CREATED"}]}
        self.config={"filters":{"entry_score":68,"quality":52,"impulse":8,"buy_ratio":1.1,"trades":5}}

    def test_never_promotes(self):
        result=analyze(self.stats,self.execution,self.config)
        self.assertFalse(result["promotion_eligible"])
        self.assertFalse(result["production_mutated"])
        self.assertEqual(result["challenger"]["status"],"UNVALIDATED")
        self.assertEqual(result["observations"]["created_events"],1)

    def test_rejects_real_execution(self):
        self.execution["paper_only"]=False
        with self.assertRaises(ValueError): analyze(self.stats,self.execution,self.config)

    def test_rejects_invalid_pnl(self):
        self.stats["cumulative_pnl_sda"]=float("nan")
        with self.assertRaises(ValueError): analyze(self.stats,self.execution,self.config)

    def test_deduplicates_completed_trades(self):
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"positions.json"
            dst=Path(d)/"archive.json"
            row={"status":"CLOSED","address":"0xabc","opened_at":"t1",
                 "closed_at":"t2","closed_profit_sda":-2.0}
            src.write_text(json.dumps({"closed_trades":[row,row,{"status":"PARTIAL CLOSED"}]}))
            self.assertEqual(export(src,dst)[0],1)
            self.assertEqual(export(src,dst)[0],1)

if __name__=="__main__": unittest.main()
