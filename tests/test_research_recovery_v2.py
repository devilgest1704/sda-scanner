import unittest
from strategy_lab.research_recovery_v2 import RecoveryObservation, evaluate_recovery_v2


class RecoveryV2Tests(unittest.TestCase):
    def sample(self, **updates):
        d = dict(age_seconds=60, trades_1h=20, buy_volume_15m=300,
                 sell_volume_15m=100, momentum_15m_pct=2,
                 previous_momentum_15m_pct=-1,
                 macd_hist_completed_1h=0.2,
                 previous_macd_hist_completed_1h=-0.1,
                 recovered_from_local_low_pct=3,
                 liquidity_verified=True, quote_valid=True)
        d.update(updates)
        return RecoveryObservation(**d)

    def test_confirmed_recovery(self):
        self.assertTrue(evaluate_recovery_v2(self.sample()).ready)

    def test_missing_macd_never_fabricates_buy(self):
        d = evaluate_recovery_v2(self.sample(macd_hist_completed_1h=None))
        self.assertFalse(d.ready)
        self.assertIn("macd_unavailable", d.reasons)

    def test_stale_quote_blocked(self):
        self.assertFalse(evaluate_recovery_v2(self.sample(age_seconds=301)).ready)

    def test_weak_pressure_blocked(self):
        self.assertFalse(evaluate_recovery_v2(self.sample(buy_volume_15m=110)).ready)

    def test_unverified_liquidity_blocked(self):
        self.assertFalse(evaluate_recovery_v2(self.sample(liquidity_verified=False)).ready)


if __name__ == "__main__":
    unittest.main()
