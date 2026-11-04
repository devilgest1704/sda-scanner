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


class TP4ContinuationTests(unittest.TestCase):
    def test_zero_previous_delta_requires_new_strong_price_confirmation(self):
        from datetime import timedelta
        previous_at = T - timedelta(seconds=100)
        previous = dict(decision_at=previous_at.isoformat(),
                        quote_at=(previous_at-timedelta(seconds=40)).isoformat(),
                        data_age_sec=40, price_sda=1, score=65, impulse=0,
                        phase="IGNITION")
        c = current(age=20, score=75, quality=70, impulse=8,
                    price_sda=1.02, m1h=5, m15=2, flow_15m=100)
        self.assertTrue(evaluate_tp4(ADDRESS,c,previous=previous,at=T)["ready"])
        for changes in (dict(impulse=0), dict(price_sda=1),
                        dict(quote_at=previous["quote_at"]), dict(flow_15m=0)):
            candidate = dict(c, **changes)
            self.assertFalse(evaluate_tp4(ADDRESS,candidate,previous=previous,at=T)["ready"])

    def test_two_fresh_quotes_admit_tp4_without_stale_recovery(self):
        from datetime import timedelta
        previous_at = T - timedelta(seconds=100)
        previous = dict(decision_at=previous_at.isoformat(),
                        quote_at=(previous_at-timedelta(seconds=40)).isoformat(),
                        data_age_sec=40, price_sda=1, score=75, impulse=8,
                        phase="CONFIRMATION")
        c = current(age=20, score=75, quality=70, impulse=8, price_sda=1.02,
                    m1h=5, m15=2, flow_15m=100)
        self.assertFalse(evaluate(ADDRESS,c,previous=previous,at=T)["ready"])
        result = evaluate_tp4(ADDRESS,c,previous=previous,at=T)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["lane"], "confirmed_continuation")
        for changes in (dict(price_sda=1), dict(data_age_sec=301), dict(flow_15m=-1),
                        dict(m15=9), dict(trades_1h=1)):
            changed = current(age=20, score=75, quality=70, impulse=8,
                              price_sda=1.02,m1h=5,m15=2,flow_15m=100)
            changed.update(changes)
            self.assertFalse(evaluate_tp4(ADDRESS,changed,previous=previous,at=T)["ready"])
