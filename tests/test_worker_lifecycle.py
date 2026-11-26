import unittest
from tools.worker_lifecycle import DEFAULT_MAX_RUNTIME_SECONDS, should_rotate


class WorkerLifecycleTests(unittest.TestCase):
    def test_worker_rotates_at_five_hour_boundary(self):
        start = 1_000_000
        self.assertFalse(should_rotate(start, start + DEFAULT_MAX_RUNTIME_SECONDS - 1))
        self.assertTrue(should_rotate(start, start + DEFAULT_MAX_RUNTIME_SECONDS))

    def test_worker_does_not_rotate_early(self):
        self.assertFalse(should_rotate(100, 100 + 60, 300))

    def test_invalid_or_future_timestamps_fail_closed(self):
        for args in ((None, 100, 300), (100, 99, 300), (100, 200, 0), (True, 200, 300)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    should_rotate(*args)


if __name__ == "__main__":
    unittest.main()
