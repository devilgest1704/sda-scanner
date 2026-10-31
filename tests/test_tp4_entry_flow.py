import unittest
from tests.test_temporal_recovery_paper_buy import T, ADDRESS, current, prior
from tools.temporal_recovery_buy import evaluate, evaluate_tp4

class TP4FlowTests(unittest.TestCase):
    def check(self, **changes):
        c = current(flow_15m=100, **changes)
        return evaluate_tp4(ADDRESS, c, previous=prior(), at=T)

    def test_safe_recovery_remains_eligible(self):
        self.assertTrue(self.check(m15=8)["ready"])

    def test_overextended_recovery_rejected_only_in_main_paper(self):
        c = current(m15=8.077154, flow_15m=100)
        self.assertTrue(evaluate(ADDRESS, c, previous=prior(), at=T)["ready"])
        self.assertEqual(self.check(m15=8.077154)["reason"], "recovery_15m_overextended")

    def test_missing_nonfinite_zero_and_selling_flow_fail_closed(self):
        for value in (None, float("nan"), float("inf"), True, 0, -1):
            with self.subTest(flow=value):
                c = current(flow_15m=value)
                self.assertFalse(evaluate_tp4(ADDRESS, c, previous=prior(), at=T)["ready"])

    def test_stale_and_missing_prior_still_rejected(self):
        self.assertFalse(self.check(age=301)["ready"])
        self.assertFalse(evaluate_tp4(ADDRESS, current(flow_15m=100), previous={}, at=T)["ready"])
