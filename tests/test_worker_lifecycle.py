import subprocess
import sys
import unittest
from pathlib import Path
from tools.worker_lifecycle import DEFAULT_MAX_RUNTIME_SECONDS, should_rotate


class WorkerLifecycleTests(unittest.TestCase):
    def test_worker_rotates_at_four_hour_boundary(self):
        start = 1_000_000
        self.assertFalse(should_rotate(start, start + DEFAULT_MAX_RUNTIME_SECONDS - 1))
        self.assertTrue(should_rotate(start, start + DEFAULT_MAX_RUNTIME_SECONDS))

    def test_worker_does_not_rotate_early(self):
        self.assertFalse(should_rotate(100, 160, 300))

    def test_invalid_or_future_timestamps_fail_closed(self):
        for args in ((None, 100, 300), (100, 99, 300), (100, 200, 0), (True, 200, 300)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    should_rotate(*args)

    def test_scanner_workflow_enforces_single_worker_concurrency_and_240m_rotation(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "scanner_v18.yml").read_text(encoding="utf-8")
        runner = (Path(__file__).resolve().parents[1] / "run_scanner_v18.sh").read_text(encoding="utf-8")
        self.assertIn("group: sda-scanner-paper", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn('SCANNER_MAX_RUNTIME_SECONDS: "14400"', workflow)
        self.assertIn('MAX_RUNTIME="${SCANNER_MAX_RUNTIME_SECONDS:-14400}"', runner)
        self.assertIn("return 1", runner)
        self.assertIn("until checkpoint_state", runner)

    def test_watchdog_authentication_health_and_single_writer_checks(self):
        watchdog = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "scanner_watchdog.yml").read_text(encoding="utf-8")
        self.assertIn("GH_TOKEN: ${{ github.token }}", watchdog)
        self.assertNotIn("secrets.GITHUB_TOKEN", watchdog)
        self.assertIn('if [ "${{ github.event_name }}" = "workflow_run" ]; then', watchdog)
        self.assertIn('event_branch" != "main"', watchdog)
        self.assertIn('event_type" = "pull_request"', watchdog)
        self.assertIn('if [ "$checkpoint_health" = "STALE" ] || [ "$checkpoint_health" = "UNKNOWN" ]', watchdog)
        self.assertNotIn('if [ "$checkpoint_health" != "HEALTHY" ]', watchdog)
        self.assertIn('if [ "$old_status" != "completed" ]', watchdog)
        self.assertIn("post_dispatch_active=", watchdog)
        self.assertIn("Scanner dispatch is queued/pending; not creating a duplicate run.", watchdog)

    def test_cli_exit_codes_match_continue_and_rotate(self):
        module = "tools.worker_lifecycle"
        before = subprocess.run([sys.executable, "-m", module, "100", "399", "300"],
                                capture_output=True, text=True)
        after = subprocess.run([sys.executable, "-m", module, "100", "400", "300"],
                               capture_output=True, text=True)
        invalid = subprocess.run([sys.executable, "-m", module, "100", "99", "300"],
                                 capture_output=True, text=True)
        self.assertEqual((before.returncode, before.stdout.strip()), (1, "CONTINUE"))
        self.assertEqual((after.returncode, after.stdout.strip()), (0, "ROTATE"))
        self.assertEqual(invalid.returncode, 2)


if __name__ == "__main__":
    unittest.main()
