import subprocess
import sys
import unittest
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
