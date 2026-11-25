import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.scanner_checkpoint_health import checkpoint_health, stale


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

    def test_fresh_stale_and_boundary(self):
        for age, expected in ((0, "HEALTHY"), (1200, "HEALTHY"), (1201, "STALE")):
            data = {"updated_at": (self.now - timedelta(seconds=age)).isoformat()}
            self.assertEqual(checkpoint_health(data, self.now), expected)
            self.assertEqual(stale(data, self.now), expected != "HEALTHY")

    def test_missing_invalid_naive_and_future_evidence_are_unknown(self):
        values = ({}, None, {"updated_at": None}, {"updated_at": "bad"},
                  {"updated_at": "2026-11-25T11:55:00"},
                  {"updated_at": (self.now + timedelta(minutes=1)).isoformat()})
        for data in values:
            with self.subTest(data=data):
                self.assertEqual(checkpoint_health(data, self.now), "UNKNOWN")
                self.assertTrue(stale(data, self.now))

    def test_unreadable_checkpoint_cli_reports_unknown(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "scanner_checkpoint_health.py"
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, str(script)], cwd=cwd,
                                    text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
