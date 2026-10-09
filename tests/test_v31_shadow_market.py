import unittest
from datetime import datetime,timezone
from experiments.v31_shadow_market import evaluate_token,run

NOW=datetime(2026,10,9,18,30,tzinfo=timezone.utc)
def token(buy=7,sell=5,timestamp="2026-10-09T18:29:00+00:00"):
    return {"last_transaction":timestamp,"flow":{"1h":{"buy_count":buy,"sell_count":sell}}}
class ShadowTests(unittest.TestCase):
    def test_eligible(self): self.assertTrue(evaluate_token(token(),NOW)["eligible"])
    def test_stale(self): self.assertIn("AGE_STALE",evaluate_token(token(timestamp="2026-10-09T18:20:00+00:00"),NOW)["reasons"])
    def test_low_activity(self): self.assertIn("LOW_ACTIVITY",evaluate_token(token(buy=5,sell=5),NOW)["reasons"])
    def test_missing(self): self.assertFalse(evaluate_token({},NOW)["eligible"])
    def test_future(self): self.assertIn("AGE_MISSING_OR_INVALID",evaluate_token(token(timestamp="2026-10-09T18:31:00+00:00"),NOW)["reasons"])
    def test_invalid_count(self): self.assertIn("TRADES_MISSING_OR_INVALID",evaluate_token(token(buy=True),NOW)["reasons"])
    def test_asof(self): self.assertEqual(run({"updated_at":NOW.isoformat(),"tokens":{"abc":token()}})["eligible"],1)
    def test_missing_asof(self):
        with self.assertRaises(ValueError): run({"tokens":{}})
if __name__=="__main__": unittest.main()
