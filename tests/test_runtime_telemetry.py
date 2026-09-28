"""Verify scanner-local report is generated without mutating the paper ledger."""
import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

class RuntimeTelemetryTests(unittest.TestCase):
    def test_report_reads_runtime_positions_and_preserves_ledger(self):
        tree=ast.parse(Path("main.py").read_text(encoding="utf-8"))
        node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=="_run_with_paper_telemetry")
        code=compile(ast.Module(body=[node],type_ignores=[]),"main.py","exec")
        class Engine:
            POSITIONS_FILE="positions.json"
            def main(self):
                Path(self.POSITIONS_FILE).write_text(json.dumps({
                    "positions":{"0xabc":{"entry_price":1,"pump_price_samples":[]}},
                    "closed_trades":[]}),encoding="utf-8")
                return "scan-complete"
        ns={"engine":Engine()}
        exec(code,ns)
        old=os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                self.assertEqual(ns["_run_with_paper_telemetry"](),"scan-complete")
                ledger=json.loads(Path("positions.json").read_text())
                result=json.loads(Path("v30_paper_telemetry_report.json").read_text())
                self.assertEqual(result["status"],"ok")
                self.assertEqual(result["open_count"],1)
                self.assertEqual(ledger["positions"]["0xabc"]["entry_price"],1)
                self.assertIn("generated_at",result)
            finally:
                os.chdir(old)

if __name__=="__main__":
    unittest.main()
