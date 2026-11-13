"""Worker code pinning: CI-approved code only, constant for the worker's life."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import worker_code_pin as pin


def git(cwd, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def write(root, rel, content):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def runs(*items):
    return lambda _url, _token: {"workflow_runs": [
        {"head_sha": sha, "status": status, "conclusion": conclusion} for sha, status, conclusion in items]}


class History:
    """main: A (code v1, tested) -> S1 (state) -> B (code v2) -> S2 (state)."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = root = Path(self.tmp.name) / "repo"
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "core.autocrlf", "false")
        git(root, "config", "user.name", "t")
        git(root, "config", "user.email", "t@example.test")
        write(root, "real_trader.py", "LIMIT = 'v1'\n")
        write(root, "swing_sell_v2.py", "POLICY = 1\n")
        write(root, "paper_champion_config.json", '{"version": "C1"}\n')
        write(root, "positions.json", '{"n": 0}\n')
        self.A = self.commit("code v1")
        write(root, "positions.json", '{"n": 1}\n')
        self.S1 = self.commit("Update SDA scanner state")
        write(root, "real_trader.py", "LIMIT = 'v2'\n")
        write(root, "tools/new_module.py", "X = 1\n")
        self.B = self.commit("code v2")
        write(root, "positions.json", '{"n": 2}\n')
        self.S2 = self.commit("Update SDA scanner state")

    def commit(self, message):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def cleanup(self):
        self.tmp.cleanup()


class WorkerCodePinTests(unittest.TestCase):
    def setUp(self):
        self.h = History()
        self.addCleanup(self.h.cleanup)
        self.root = self.h.root

    def do_pin(self, fetch, run_id="101"):
        return pin.pin(cwd=self.root, repo="o/r", token="t", run_id=run_id, fetch=fetch)

    def test_failed_ci_pins_worker_to_last_approved_code(self):
        record = self.do_pin(runs((self.h.B, "completed", "failure"), (self.h.A, "completed", "success")))
        self.assertEqual(record["worker_code_sha"], self.h.A)
        self.assertIn("PINNED_TO_LAST_APPROVED", record["worker_code_approval"])
        self.assertEqual(Path(self.root, "real_trader.py").read_text(), "LIMIT = 'v1'\n")
        self.assertFalse(Path(self.root, "tools/new_module.py").exists())
        # State stays at main HEAD.
        self.assertEqual(Path(self.root, "positions.json").read_text(), '{"n": 2}\n')
        self.assertEqual(pin.verify(self.root)["code_integrity"], "OK")

    def test_green_ci_allows_rollout_of_new_code(self):
        record = self.do_pin(runs((self.h.S2, "completed", "success"), (self.h.A, "completed", "success")))
        self.assertEqual(record["worker_code_sha"], self.h.B)
        self.assertEqual(record["worker_code_approval"], "APPROVED")
        self.assertEqual(Path(self.root, "real_trader.py").read_text(), "LIMIT = 'v2'\n")

    def test_pending_ci_does_not_roll_out(self):
        record = self.do_pin(runs((self.h.B, "in_progress", None), (self.h.A, "completed", "success")))
        self.assertEqual(record["worker_code_sha"], self.h.A)
        self.assertIn("PENDING", record["worker_code_approval"])

    def test_no_approved_code_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "No CI-approved"):
            self.do_pin(runs((self.h.B, "completed", "failure")))
        with self.assertRaisesRegex(RuntimeError, "GH_TOKEN"):
            pin.pin(cwd=self.root, repo="o/r", token=None, fetch=runs())
        with self.assertRaises(RuntimeError):
            self.do_pin(lambda _u, _t: {"message": "API rate limit"})

    def test_state_only_commits_keep_code_approved(self):
        approval = pin.resolve_approval("HEAD", runs((self.h.S2, "completed", "success"))(None, None)["workflow_runs"],
                                        cwd=self.root)
        self.assertEqual(approval["approved_sha"], self.h.B)
        self.assertEqual(approval["head_state"], "APPROVED")
        self.assertEqual(pin.code_fingerprint(self.h.A, self.root), pin.code_fingerprint(self.h.S1, self.root))
        self.assertNotEqual(pin.code_fingerprint(self.h.A, self.root), pin.code_fingerprint(self.h.B, self.root))

    def test_worker_sha_is_fixed_and_drift_is_detected(self):
        record = self.do_pin(runs((self.h.A, "completed", "success")))
        self.assertEqual(pin.load_record(self.root)["worker_code_sha"], record["worker_code_sha"])
        # Runtime state changes are not code drift.
        write(self.root, "positions.json", '{"n": 99}\n')
        write(self.root, "__pycache__/x.cpython-311.pyc", "bytecode")
        self.assertEqual(pin.verify(self.root)["code_integrity"], "OK")
        write(self.root, "real_trader.py", "LIMIT = 'hot swapped'\n")
        result = pin.verify(self.root)
        self.assertEqual(result["code_integrity"], "CODE_SHA_DRIFT")
        self.assertIn("real_trader.py", result["code_drift"][0])
        self.assertEqual(pin.load_record(self.root)["worker_code_sha"], self.h.A)

    def test_head_movement_is_code_drift(self):
        self.do_pin(runs((self.h.S2, "completed", "success")))
        git(self.root, "reset", "-q", "--hard", self.h.S1)
        self.assertEqual(pin.verify(self.root)["code_integrity"], "CODE_SHA_DRIFT")

    def test_status_reports_pinned_identity_and_health(self):
        self.do_pin(runs((self.h.S2, "completed", "success")), run_id="555")
        status = pin.write_status(cwd=self.root)
        saved = json.loads(Path(self.root, pin.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved, status)
        self.assertEqual(status["worker_code_sha"], self.h.B)
        self.assertEqual(status["worker_run_id"], "555")
        self.assertTrue(status["worker_started_at"])
        self.assertEqual(status["health"], "OK")
        write(self.root, "swing_sell_v2.py", "POLICY = 2\n")
        self.assertEqual(pin.write_status(cwd=self.root)["health"], "CODE_SHA_DRIFT")

    def test_status_persists_runtime_freshness_and_degraded_health(self):
        self.do_pin(runs((self.h.S2, "completed", "success")), run_id="556")
        stamp = pin._now()
        write(self.root, "market_data.json", json.dumps({"updated_at": "2026-10-07T15:05:14+00:00"}))
        write(self.root, "market_source_state.json", json.dumps({
            "source_health": "HEALTHY", "last_successful_market_at": stamp}))
        write(self.root, "market_analysis.json", json.dumps({"updated_at": stamp}))
        write(self.root, "whale_state.json", json.dumps({
            "updated_at": stamp, "last_successful_whale_at": stamp,
            "whale_source_health": "HEALTHY"}))
        write(self.root, "paper_stats.json", json.dumps({"updated_at": stamp}))
        write(self.root, "real_trade_state.json", json.dumps({
            "updated_at": stamp, "positions": {"a": {}, "b": {}}}))
        write(self.root, "real_paper_bridge_state.json", json.dumps({
            "last_run": stamp, "last_real_reconciliation_at": stamp,
            "last_real_sell_evaluation_at": stamp, "real_positions_evaluated": 2,
            "sell_health": {"at": stamp, "evaluated": 2, "health": "SELL_HOLD"}}))
        write(self.root, "strategy_lab/lane_research_state.json", json.dumps({"updated_at": stamp}))

        status = pin.write_status(cwd=self.root)
        self.assertEqual(status["last_successful_scan_at"], stamp)
        self.assertEqual(status["last_successful_market_at"], stamp)
        self.assertEqual(status["last_successful_market_analysis_at"], stamp)
        self.assertEqual(status["market_source_health"], "HEALTHY")
        self.assertEqual(status["last_successful_whale_at"], stamp)
        self.assertEqual(status["last_real_sell_evaluation_at"], stamp)
        self.assertEqual(status["last_real_reconciliation_at"], stamp)
        self.assertEqual(status["last_lane_research_update_at"], stamp)
        self.assertEqual(status["real_positions_evaluated"], 2)
        self.assertEqual(status["real_sell_protection"], "ACTIVE")
        self.assertEqual(status["worker_health"], "HEALTHY")

        write(self.root, "market_source_state.json", json.dumps({
            "source_health": "SOURCE_UNAVAILABLE", "last_successful_market_at": stamp,
            "last_error_http_status": 521}))
        status = pin.write_status(cwd=self.root)
        self.assertEqual(status["market_source_health"], "SOURCE_UNAVAILABLE")
        self.assertEqual(status["worker_health"], "BUY_DATA_DEGRADED")

        write(self.root, "whale_state.json", json.dumps({
            "updated_at": stamp, "last_successful_whale_at": stamp,
            "whale_source_health": "SOURCE_UNAVAILABLE", "last_error_http_status": 521}))
        status = pin.write_status(cwd=self.root)
        self.assertEqual(status["whale_source_health"], "SOURCE_UNAVAILABLE")
        self.assertEqual(status["worker_health"], "BUY_DATA_DEGRADED")

    def test_unpinned_checkout_is_reported_not_drift(self):
        self.assertEqual(pin.verify(self.root)["code_integrity"], "UNPINNED")


class WatchdogRolloutTests(unittest.TestCase):
    def setUp(self):
        self.h = History()
        self.addCleanup(self.h.cleanup)
        self.root = self.h.root

    def publish_worker(self, code_rev, run_id="7"):
        expected = pin.fingerprint(pin.expected_entries(code_rev, "HEAD", cwd=self.root))
        write(self.root, pin.STATUS_FILE, json.dumps({"worker_run_id": run_id,
                                                      "worker_pinned_fingerprint": expected}))

    def decide(self, fetch, run_id="7", run_sha=None, age=3600):
        return pin.watchdog_decision(run_id, run_sha or self.h.A, age, cwd=self.root,
                                     repo="o/r", token="t", fetch=fetch)[0]

    def test_failed_ci_never_replaces_worker(self):
        self.publish_worker(self.h.A)
        self.assertEqual(self.decide(runs((self.h.B, "completed", "failure"),
                                          (self.h.A, "completed", "success"))), "keep")

    def test_pending_ci_never_replaces_worker(self):
        self.publish_worker(self.h.A)
        self.assertEqual(self.decide(runs((self.h.B, "queued", None),
                                          (self.h.A, "completed", "success"))), "keep")

    def test_green_ci_replaces_worker_with_new_expected_sha(self):
        self.publish_worker(self.h.A)
        fetch = runs((self.h.B, "completed", "success"), (self.h.A, "completed", "success"))
        self.assertEqual(self.decide(fetch), "replace")
        # The replacement worker then pins exactly the newly approved code.
        record = pin.pin(cwd=self.root, repo="o/r", token="t", run_id="8", fetch=fetch)
        self.assertEqual(record["worker_code_sha"], self.h.B)
        self.publish_worker(self.h.B, run_id="8")
        self.assertEqual(self.decide(fetch, run_id="8"), "keep")

    def test_state_only_commits_do_not_require_rollout(self):
        self.publish_worker(self.h.B)
        self.assertEqual(self.decide(runs((self.h.B, "completed", "success"))), "keep")

    def test_champion_config_change_requires_controlled_restart(self):
        self.publish_worker(self.h.B)
        write(self.root, "paper_champion_config.json", '{"version": "C2"}\n')
        self.h.commit("PAPER champion promotion")
        self.assertEqual(self.decide(runs((self.h.B, "completed", "success"))), "replace")

    def test_unpublished_young_worker_is_kept_and_legacy_worker_compared(self):
        fetch = runs((self.h.B, "completed", "success"))
        self.assertEqual(self.decide(fetch, run_id="other", age=400), "keep")
        self.assertEqual(self.decide(fetch, run_id="other", run_sha=self.h.A, age=4000), "replace")
        self.assertEqual(self.decide(fetch, run_id="other", run_sha=self.h.S2, age=4000), "keep")

    def test_unverifiable_approval_raises_for_watchdog_to_keep(self):
        with self.assertRaises(RuntimeError):
            self.decide(lambda _u, _t: {})


if __name__ == "__main__":
    unittest.main()
