"""Regression: 60m learner labels must never be filled from 90-120m quotes."""
import unittest
from datetime import datetime, timedelta, timezone
from tools.candidate_price_tracker import update
from tools.missed_pump_learning import normalize

START=datetime(2026,10,15,0,0,tzinfo=timezone.utc)
ADDRESS="0x"+"1"*40

def candidate():
    return {
        "at":START.isoformat(), "address":ADDRESS, "quote_at":(START-timedelta(seconds=30)).isoformat(),
        "price_sda":1.0, "phase":"WATCH", "eligible":False, "gates":["score"],
        "score":76, "quality":70, "snapshot_impulse":12,
        "m1h":4, "m15":2.5, "m4h":12,
        "trades_1h":8, "buy_ratio":2.2, "volume_1h":1500,
        "flow_1h":800, "flow_15m":500,
    }

def market(minutes,price):
    stamp=START+timedelta(minutes=minutes)
    return {
        "updated_at":stamp.isoformat(),
        "tokens":{ADDRESS:{"analysis":{
            "last_transaction":stamp.isoformat(), "price_in_sda":price
        }}},
    }

class HorizonIntegrityTest(unittest.TestCase):
    def test_late_quote_is_not_mislabeled_as_60m(self):
        state={"funnels":[{"at":START.isoformat(),"candidate_snapshots":[candidate()]}]}
        late=update(state,market(95,1.5))
        row=late["observations"][0]
        self.assertNotIn("60m",row["outcomes"])
        self.assertIn("observed_peak_240m",row)
        self.assertEqual(len(normalize(late["observations"])),0)

    def test_timely_quote_is_usable_for_causal_learning(self):
        state={"funnels":[{"at":START.isoformat(),"candidate_snapshots":[candidate()]}]}
        timely=update(state,market(65,1.12))
        self.assertEqual(timely["observations"][0]["outcomes"]["60m"]["elapsed_minutes"],65)
        self.assertEqual(len(normalize(timely["observations"])),1)

    def test_no_later_quote_overwrites_earlier_valid_60m(self):
        state={"funnels":[{"at":START.isoformat(),"candidate_snapshots":[candidate()]}]}
        first=update(state,market(62,1.08))
        later=update({"observations":first["observations"]},market(110,1.6))
        self.assertEqual(later["observations"][0]["outcomes"]["60m"]["price_sda"],1.08)
        self.assertEqual(len(normalize(later["observations"])),1)

if __name__=="__main__":
    unittest.main()
