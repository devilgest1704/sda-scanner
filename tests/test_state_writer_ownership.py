"""Protect durable PAPER scanner state from competing GitHub Actions commits.

Regression for 2026-10-14/15: 62 consecutive scanner state checkpoints failed
on strategy_lab/evolution_state.json and missed_pump_learning.json because the
hourly autonomous agent wrote those SAME files to main. The scanner's research
and PAPER data were then unavailable to downstream dashboard/optimizer.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER_OWNED_RESEARCH = {
    "strategy_lab/candidate_outcomes.json",
    "strategy_lab/missed_pump_learning.json",
    "strategy_lab/evolution_state.json",
    "strategy_lab/forward_shadow_state.json",
}
AUTONOMOUS_OWNED = {
    "strategy_lab/autonomous_agent_report.json",
    "v30_agent_config.json",
}


def staged_files_from_workflow(name):
    source = (ROOT / ".github" / "workflows" / name).read_text(
        encoding="utf-8")
    found = set()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("git add -f "):
            found.update(stripped.removeprefix("git add -f ").split())
    return found


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True,
        text=True, check=True).stdout.strip()


def save(directory, relpath, content):
    path = directory / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class SingleWriterCheckpointTests(unittest.TestCase):
    def test_only_scanner_commits_candidate_and_evolution_state(self):
        runner = (ROOT / "run_scanner_v18.sh").read_text(encoding="utf-8")
        marker = next(
            line for line in runner.splitlines()
            if line.startswith("STATE_FILES="))
        for path in SCANNER_OWNED_RESEARCH:
            self.assertIn(path, marker)
        agent_files = staged_files_from_workflow("autonomous_paper_agent.yml")
        self.assertEqual(agent_files, AUTONOMOUS_OWNED)
        self.assertTrue(SCANNER_OWNED_RESEARCH.isdisjoint(agent_files))
        lab_files = staged_files_from_workflow("strategy-lab.yml")
        self.assertTrue(SCANNER_OWNED_RESEARCH.isdisjoint(lab_files))
        self.assertIn("strategy_lab/autonomous_agent_report.json", agent_files)
        self.assertIn("v30_agent_config.json", agent_files)

    def test_simulated_concurrent_agent_commit_no_longer_conflicts(self):
        # Real git push/rebase path, but on temporary LOCAL repositories only.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, scanner, agent, observer = (
                root / "remote.git", root / "scanner", root / "agent",
                root / "observer")
            subprocess.run(["git", "init", "--bare", str(remote)],
                           capture_output=True, check=True)
            subprocess.run(["git", "clone", str(remote), str(scanner)],
                           capture_output=True, check=True)
            git(scanner, "config", "user.name", "test scanner")
            git(scanner, "config", "user.email", "scanner@example.test")
            for p in SCANNER_OWNED_RESEARCH | AUTONOMOUS_OWNED:
                save(scanner, p, '{"version":1}\n')
            git(scanner, "add", ".")
            git(scanner, "commit", "-m", "baseline")
            git(scanner, "push", "origin", "HEAD:main")
            subprocess.run(["git", "-C", str(remote), "symbolic-ref",
                            "HEAD", "refs/heads/main"], check=True)
            subprocess.run(["git", "clone", str(remote), str(agent)],
                           capture_output=True, check=True)
            git(agent, "config", "user.name", "test agent")
            git(agent, "config", "user.email", "agent@example.test")
            save(scanner, "strategy_lab/evolution_state.json",
                 '{"version":2,"paper_only":true}\n')
            save(scanner, "strategy_lab/missed_pump_learning.json",
                 '{"version":2,"source":"scanner"}\n')
            git(scanner, "add", "strategy_lab/evolution_state.json",
                "strategy_lab/missed_pump_learning.json")
            git(scanner, "commit", "-m", "scanner state")
            save(agent, "strategy_lab/autonomous_agent_report.json",
                 '{"version":2,"source":"autonomous"}\n')
            git(agent, "add", "strategy_lab/autonomous_agent_report.json")
            git(agent, "commit", "-m", "agent report")
            git(agent, "push", "origin", "HEAD:main")
            git(scanner, "fetch", "origin", "main")
            git(scanner, "rebase", "origin/main")
            git(scanner, "push", "origin", "HEAD:main")
            subprocess.run(["git", "clone", str(remote), str(observer)],
                           capture_output=True, check=True)
            self.assertIn('"paper_only":true',(
                observer/"strategy_lab/evolution_state.json").read_text())
            self.assertIn('"source":"scanner"',(
                observer/"strategy_lab/missed_pump_learning.json").read_text())
            self.assertIn('"source":"autonomous"',(
                observer/"strategy_lab/autonomous_agent_report.json").read_text())

            # The lab may modify a sampler checkpoint during research/tests.
            # It must preserve that local work without rebasing or publishing
            # it over a newer authoritative scanner checkpoint.
            candidate = "strategy_lab/candidate_outcomes.json"
            save(scanner, candidate, '{"version":2,"source":"scanner"}\n')
            git(scanner, "add", candidate)
            git(scanner, "commit", "-m", "new scanner checkpoint")
            git(scanner, "push", "origin", "HEAD:main")
            save(agent, candidate, '{"version":3,"source":"unpublished_lab"}\n')
            save(agent, "strategy_lab/report.json", '{"research":true}\n')
            git(agent, "add", "strategy_lab/report.json")
            git(agent, "commit", "-m", "reviewed research")
            git(agent, "stash", "push", "--include-untracked", "-m", "unpublished-state")
            git(agent, "fetch", "origin", "main")
            git(agent, "rebase", "origin/main")
            git(agent, "push", "origin", "HEAD:main")
            git(observer, "pull", "--ff-only")
            self.assertIn('"source":"scanner"', (observer/candidate).read_text())
            self.assertEqual((observer/"strategy_lab/report.json").read_text(), '{"research":true}\n')
            self.assertIn('unpublished_lab', git(agent, "show", "stash:"+candidate))


if __name__ == "__main__":
    unittest.main()
