"""State checkpoints publish JSON only and never change the running worker's code."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import state_checkpoint as sc


def git(cwd, *args):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def write(root, rel, content):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def clone(remote, target, name):
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(remote), str(target)],
                   capture_output=True, check=True)
    git(target, "config", "core.autocrlf", "false")
    git(target, "config", "user.name", name)
    git(target, "config", "user.email", f"{name}@example.test")


class Repo:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote, self.worker, self.dev = root / "remote.git", root / "worker", root / "dev"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       capture_output=True, check=True)
        seed = root / "seed"
        clone(self.remote, seed, "seed")
        git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        write(seed, "real_paper_bridge.py", "VERSION = 1\n")
        write(seed, "run_scanner_v18.sh", "echo v1\n")
        write(seed, "positions.json", '{"positions": {}}\n')
        write(seed, "real_trade_state.json", '{"positions": {}}\n')
        write(seed, "strategy_lab/paper_paths/a.json", '{"obs": 1}\n')
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "baseline")
        git(seed, "push", "-q", "origin", "main")
        clone(self.remote, self.worker, "worker")
        clone(self.remote, self.dev, "dev")
        self.start_head = git(self.worker, "rev-parse", "HEAD")

    def remote_file(self, rel):
        return git(self.remote, "show", f"main:{rel}")

    def cleanup(self):
        self.tmp.cleanup()


class StateCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def push(self, paths, **kw):
        return sc.push_state(paths, "Update SDA scanner state", cwd=self.repo.worker,
                             sleep=lambda _s: None, **kw)

    def test_state_is_published_while_head_and_code_stay_pinned(self):
        write(self.repo.worker, "positions.json", '{"positions": {"0xabc": {}}}\n')
        result = self.push(["positions.json", "real_trade_state.json"])
        self.assertEqual(result["status"], "PUSHED")
        self.assertEqual(result["paths"], ["positions.json"])
        self.assertIn("0xabc", self.repo.remote_file("positions.json"))
        self.assertEqual(git(self.repo.worker, "rev-parse", "HEAD"), self.repo.start_head)
        self.assertEqual(git(self.repo.worker, "status", "--porcelain", "--", "real_paper_bridge.py"), "")

    def test_new_main_code_never_enters_the_running_worker(self):
        write(self.repo.dev, "real_paper_bridge.py", "VERSION = 2\n")
        write(self.repo.dev, "run_scanner_v18.sh", "echo v2\n")
        git(self.repo.dev, "commit", "-q", "-am", "new code")
        git(self.repo.dev, "push", "-q", "origin", "main")
        write(self.repo.worker, "real_trade_state.json", '{"positions": {"0xdef": {}}}\n')
        result = self.push(["positions.json", "real_trade_state.json"])
        self.assertEqual(result["status"], "PUSHED")
        # Remote keeps the new code AND receives the worker's state.
        self.assertEqual(self.repo.remote_file("real_paper_bridge.py"), "VERSION = 2")
        self.assertIn("0xdef", self.repo.remote_file("real_trade_state.json"))
        # The running worker still executes the code it started with.
        self.assertEqual(Path(self.repo.worker, "real_paper_bridge.py").read_text(), "VERSION = 1\n")
        self.assertEqual(Path(self.repo.worker, "run_scanner_v18.sh").read_text(), "echo v1\n")
        self.assertEqual(git(self.repo.worker, "rev-parse", "HEAD"), self.repo.start_head)

    def test_executable_paths_are_refused(self):
        for path in ("real_paper_bridge.py", "run_scanner_v18.sh", "paper_champion_config.json",
                     ".github/workflows/scanner_v18.yml"):
            write(self.repo.worker, path, "x\n")
            with self.assertRaises(ValueError):
                self.push([path])
        self.assertEqual(self.repo.remote_file("real_paper_bridge.py"), "VERSION = 1")

    def test_directory_state_is_expanded_to_json_files_only(self):
        write(self.repo.worker, "strategy_lab/paper_paths/b.json", '{"obs": 2}\n')
        write(self.repo.worker, "strategy_lab/paper_paths/evil.py", "import os\n")
        result = self.push(["strategy_lab/paper_paths"])
        self.assertEqual(result["paths"], ["strategy_lab/paper_paths/b.json"])
        listing = git(self.repo.remote, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("evil.py", listing)

    def test_concurrent_change_to_same_state_file_is_a_safe_conflict(self):
        write(self.repo.dev, "positions.json", '{"positions": {"other": {}}}\n')
        git(self.repo.dev, "commit", "-q", "-am", "other writer")
        git(self.repo.dev, "push", "-q", "origin", "main")
        remote_before = git(self.repo.remote, "rev-parse", "main")
        write(self.repo.worker, "positions.json", '{"positions": {"mine": {}}}\n')
        result = self.push(["positions.json"])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(git(self.repo.remote, "rev-parse", "main"), remote_before)
        self.assertEqual(git(self.repo.worker, "rev-parse", "HEAD"), self.repo.start_head)
        self.assertIn("mine", Path(self.repo.worker, "positions.json").read_text())

    def test_successive_checkpoints_use_last_published_state_as_base(self):
        write(self.repo.worker, "positions.json", '{"n": 1}\n')
        self.assertEqual(self.push(["positions.json"])["status"], "PUSHED")
        write(self.repo.worker, "positions.json", '{"n": 2}\n')
        self.assertEqual(self.push(["positions.json"])["status"], "PUSHED")
        self.assertEqual(json.loads(self.repo.remote_file("positions.json")), {"n": 2})
        self.assertEqual(self.push(["positions.json"])["status"], "NOOP")

    def test_real_intent_requires_a_new_durable_change(self):
        with self.assertRaises(sc.StateUnchanged):
            self.push(["real_trade_state.json"], require_changed=["real_trade_state.json"])
        write(self.repo.worker, "real_trade_state.json", '{"pending_buys": {"0x1": {}}}\n')
        result = self.push(["real_trade_state.json"], require_changed=["real_trade_state.json"])
        self.assertEqual(result["status"], "PUSHED")

    def test_push_failure_publishes_nothing_and_keeps_code(self):
        write(self.repo.worker, "positions.json", '{"n": 3}\n')
        result = sc.push_state(["positions.json"], cwd=self.repo.worker, remote="missing-remote",
                               attempts=2, sleep=lambda _s: None)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(git(self.repo.worker, "rev-parse", "HEAD"), self.repo.start_head)


if __name__ == "__main__":
    unittest.main()
