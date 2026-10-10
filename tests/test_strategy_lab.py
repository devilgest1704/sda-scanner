import json
import tempfile
import unittest
from pathlib import Path
from tools.strategy_lab import analyze, attach_research_evidence
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

    def test_research_report_distinguishes_snapshots_from_price_outcomes(self):
        report=analyze(self.stats,self.execution,self.config)
        research={
            "recommended_entry_score":76,"sufficient_sample":True,
            "sample_count":468,"train_sample_count":327,"holdout_sample_count":141,
            "holdout_observed_retained_trades":40,
            "train_observed_difference_sda":90,
            "holdout_observed_difference_sda":-12
        }
        candidates={
            "updated_at":"2026-10-10T13:00:00+00:00",
            "observations":[
                {"at":"2026-10-10T12:00:00+00:00","address":"0xabc","eligible":False,"outcomes":{}},
                {"at":"2026-10-10T12:50:00+00:00","address":"0xabc","eligible":True,"outcomes":{}},
                {"at":"2026-10-10T12:00:00+00:00","address":"0xdef","eligible":False,
                 "outcomes":{"15m":{"price_return_pct":5}}}
            ]
        }
        attach_research_evidence(report,research,candidates)
        self.assertEqual(report["challenger"]["filters"]["entry_score"],76)
        self.assertEqual(report["candidate_evidence"]["candidate_snapshots"],3)
        self.assertEqual(report["candidate_evidence"]["distinct_tokens"],2)
        self.assertEqual(report["candidate_evidence"]["entry_eligible_snapshots"],1)
        self.assertEqual(report["candidate_evidence"]["matured_15m"],2)
        self.assertEqual(report["candidate_evidence"]["observed_15m"],1)
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["production_mutated"])
        self.assertEqual(report["challenger"]["status"],"UNVALIDATED")

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
