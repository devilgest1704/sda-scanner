"""REAL bridge isolation: PAPER files must be irrelevant to REAL reconciliation and SELL."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_real_sell_p0_fixes import BridgeHarness, FakeChain, TOKEN


class RealBridgePaperIsolationTests(unittest.TestCase):
    def test_malformed_paper_and_shadow_files_are_never_read(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.paper.write_text("{malformed", encoding="utf-8")
        shadow = Path(chain.tmp.name) / "strategy_lab" / "forward_shadow_state.json"
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text(json.dumps({"cooldowns": {TOKEN: "2099-01-01T00:00:00+00:00"},
                                      "loss_circuit": {"blocked": True}}), encoding="utf-8")
        original_read = Path.read_text
        reads = []

        def guarded_read(path, *args, **kwargs):
            if path.name in {"positions.json", "forward_shadow_state.json"}:
                reads.append(path.name)
                raise AssertionError("REAL bridge attempted to read PAPER state")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded_read):
            result = harness.run()

        self.assertEqual(reads, [])
        self.assertEqual(result["paper_state"], "NOT_USED")
        self.assertEqual(result["status"], "reconciled")
        harness.buy.assert_not_called()

    def test_missing_paper_ledger_does_not_block_real_sell_reconciliation(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.paper.unlink()
        result = harness.run()
        self.assertEqual(result["paper_state"], "NOT_USED")
        self.assertEqual(result["status"], "reconciled")
        harness.buy.assert_not_called()

    def test_real_pending_buy_is_not_created_or_replayed_by_bridge(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        chain.state.setdefault("pending_buys", {})[TOKEN] = {"position_sda": 30.0}
        harness.paper.unlink()
        result = harness.run()
        self.assertEqual(result["paper_state"], "NOT_USED")
        harness.buy.assert_not_called()
        self.assertIn(TOKEN, chain.state["pending_buys"])


if __name__ == "__main__":
    unittest.main()
