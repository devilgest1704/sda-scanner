import unittest
from datetime import datetime, timezone, timedelta
from tools.scanner_checkpoint_health import stale

class HealthTests(unittest.TestCase):
    def test_fresh_stale_and_boundary(self):
        now = datetime(2026,11,1,tzinfo=timezone.utc)
        for age, expected in ((0,False),(1200,False),(1201,True),(-60,False)):
            self.assertEqual(stale({"updated_at":(now-timedelta(seconds=age)).isoformat()},now),expected)

    def test_unknown_evidence_does_not_restart(self):
        now = datetime.now(timezone.utc)
        for data in ({},None,{"updated_at":None},{"updated_at":"bad"},{"updated_at":"2026-11-01T00:00:00"}):
            self.assertFalse(stale(data,now))
