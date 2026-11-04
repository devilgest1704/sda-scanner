import unittest
from datetime import timedelta
from tests.test_temporal_recovery_paper_buy import T, ADDRESS, current
from tools.temporal_recovery_buy import evaluate_tp4_legacy as evaluate_tp4, evaluate_paper

class RetainedConfirmationTests(unittest.TestCase):
    def test_retained_witness_requires_new_current_confirmation(self):
        observed = T - timedelta(seconds=120)
        witness = dict(decision_at=observed.isoformat(),
                       quote_at=(observed-timedelta(seconds=20)).isoformat(),
                       data_age_sec=20, price_sda=1, score=65, impulse=0, phase="IGNITION")
        prior = dict(witness, score=20, phase="NO", paper_confirmation=witness)
        candidate = current(age=20, score=75, quality=70, impulse=8,
                            price_sda=1.02,m1h=5,m15=2,flow_15m=100)
        self.assertFalse(evaluate_paper(ADDRESS,candidate,previous=prior,at=T)["ready"])
        self.assertTrue(evaluate_tp4(ADDRESS,candidate,previous=prior,at=T)["ready"])
        for changes in (dict(impulse=0),dict(price_sda=1),dict(flow_15m=-1),
                        dict(data_age_sec=301),dict(m15=9)):
            self.assertFalse(evaluate_tp4(ADDRESS,dict(candidate,**changes),previous=prior,at=T)["ready"])
        expired = dict(witness,decision_at=(T-timedelta(seconds=901)).isoformat())
        self.assertFalse(evaluate_tp4(ADDRESS,candidate,
                                     previous=dict(prior,paper_confirmation=expired),at=T)["ready"])
