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

    def _status(self, scan_age=0, heartbeat_age=0):
        return {
            "last_successful_scan_at": (self.now - timedelta(seconds=scan_age)).isoformat(),
            "lastWorkerHeartbeat": (self.now - timedelta(seconds=heartbeat_age)).isoformat(),
        }

    def test_completed_scan_and_heartbeat_freshness_boundaries(self):
        self.assertEqual(checkpoint_health(self._status(), self.now), "HEALTHY")
        self.assertEqual(checkpoint_health(self._status(scan_age=1200), self.now), "HEALTHY")
        self.assertEqual(checkpoint_health(self._status(scan_age=1201), self.now), "IN_PROGRESS")
        self.assertFalse(stale(self._status(scan_age=1201), self.now))
        self.assertEqual(checkpoint_health(self._status(scan_age=0, heartbeat_age=901), self.now), "STALE")
        self.assertTrue(stale(self._status(scan_age=0, heartbeat_age=901), self.now))

    def test_fresh_heartbeat_allows_startup_or_scan_in_progress_without_live_label(self):
        data = {"lastWorkerHeartbeat": self.now.isoformat()}
        self.assertEqual(checkpoint_health(data, self.now), "IN_PROGRESS")
        self.assertFalse(stale(data, self.now))

    def test_missing_invalid_naive_and_future_evidence_fail_closed(self):
        values = (
            {}, None,
            {"last_successful_scan_at": self.now.isoformat()},
            {"lastWorkerHeartbeat": "bad", "last_successful_scan_at": self.now.isoformat()},
            {"lastWorkerHeartbeat": "2026-11-25T11:55:00", "last_successful_scan_at": self.now.isoformat()},
            {"lastWorkerHeartbeat": (self.now + timedelta(minutes=1)).isoformat(),
             "last_successful_scan_at": self.now.isoformat()},
            {"lastWorkerHeartbeat": self.now.isoformat(), "last_successful_scan_at": "bad"},
            {"lastWorkerHeartbeat": self.now.isoformat(), "last_successful_scan_at": "2026-11-25T11:55:00"},
            {"lastWorkerHeartbeat": self.now.isoformat(),
             "last_successful_scan_at": (self.now + timedelta(minutes=1)).isoformat()},
        )
        for data in values:
            with self.subTest(data=data):
                self.assertEqual(checkpoint_health(data, self.now), "UNKNOWN")
                self.assertTrue(stale(data, self.now))

    def test_updated_at_alone_is_not_worker_health(self):
        self.assertEqual(checkpoint_health({"updated_at": self.now.isoformat()}, self.now), "UNKNOWN")

    def test_unreadable_worker_status_cli_reports_unknown(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "scanner_checkpoint_health.py"
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, str(script)], cwd=cwd,
                                    text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
