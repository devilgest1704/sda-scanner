import unittest
from datetime import datetime, timedelta, timezone
from tools.v31_schedule_guard import recovery_needed


def run(when, status="completed", conclusion="success", event="schedule"):
    return {"createdAt": when.isoformat(), "status": status,
            "conclusion": conclusion, "event": event}


class V31DailyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.target = datetime(2026, 10, 12, 3, 17, tzinfo=timezone.utc)

    def test_do_not_dispatch_before_grace(self):
        self.assertFalse(recovery_needed(self.target + timedelta(minutes=44),
                                         [run(self.target-timedelta(days=1))]))

    def test_recover_missing_daily_cron(self):
        self.assertTrue(recovery_needed(self.target + timedelta(minutes=48),
                                        [run(self.target-timedelta(days=1))]))

    def test_successful_manual_or_push_run_counts_as_today(self):
        now = self.target + timedelta(hours=3)
        for event in ("workflow_dispatch", "schedule", "push"):
            self.assertFalse(recovery_needed(now, [run(self.target + timedelta(minutes=2),
                                                       event=event)]))

    def test_active_and_queued_never_double_dispatch(self):
        now = self.target + timedelta(hours=3)
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            self.assertFalse(recovery_needed(now, [
                run(self.target+timedelta(hours=1), status, None, "workflow_dispatch")
            ]))

    def test_failed_run_retry_only_after_cooldown(self):
        failed = run(self.target + timedelta(minutes=50),
                     conclusion="failure")
        self.assertFalse(recovery_needed(self.target + timedelta(minutes=70), [failed]))
        self.assertTrue(recovery_needed(self.target + timedelta(minutes=111), [failed]))

    def test_no_future_or_pull_request_counts(self):
        now = self.target + timedelta(hours=3)
        self.assertTrue(recovery_needed(now, [
            run(self.target+timedelta(days=1)),
            run(self.target+timedelta(minutes=20), event="pull_request")
        ]))

    def test_bad_or_missing_data_fails_closed(self):
        now = self.target+timedelta(hours=3)
        self.assertFalse(recovery_needed(now, []))
        self.assertFalse(recovery_needed(now, {}))
        self.assertFalse(recovery_needed(now.replace(tzinfo=None), [{"event": "schedule"}]))


if __name__ == "__main__":
    unittest.main()
