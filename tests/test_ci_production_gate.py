"""Static production-safety gates for the REAL worker, CI and Vercel deployment."""
import re
import unittest
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CRITICAL = ["real_paper_bridge.py", "real_market_price.py", "real_swing_sell_executor.py",
            "swing_sell_v2.py", "real_trader.py", "real_trading_config.py", "v30_pump_hunter.py",
            "tools/temporal_recovery_buy.py", "paper_swing_sell_v2.py", "paper_engine_v19.py",
            "engine_legacy.py", "main.py", "paper_champion_config.py", "run_scanner_v18.sh",
            "requirements.txt", ".github/workflows/scanner_v18.yml", "tools/worker_code_pin.py",
            "tools/state_checkpoint.py", "api/index.py"]
CODE_MUTATING_GIT = re.compile(r"\bgit\b[^\n]*\b(rebase|pull|reset|checkout|stash|merge|switch|restore)\b")


def text(path):
    return Path(path).read_text(encoding="utf-8")


class RealTestGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = text(WORKFLOWS / "real-bridge-tests.yml")

    def test_every_critical_code_path_triggers_the_gate_but_state_json_does_not(self):
        push = self.gate.split("push:", 1)[1].split("jobs:", 1)[0]
        patterns = [p.strip().strip('"') for p in re.findall(r"^\s*-\s*(.+)$", push, re.M)
                    if not p.strip().strip('"').startswith(("main", "feature/"))]
        glob = lambda path: any(fnmatch(path, p.replace("**", "*")) for p in patterns)  # noqa: E731
        for path in CRITICAL:
            self.assertTrue(glob(path), path)
        for state in ("positions.json", "real_trade_state.json", "real_worker_status.json",
                      "strategy_lab/paper_paths/x.json", "paper_stats.json"):
            self.assertFalse(glob(state), state)

    def test_no_continue_on_error_in_the_production_gate(self):
        self.assertNotIn("continue-on-error", self.gate)
        self.assertIn("tests/test_*.py", self.gate)
        self.assertIn("tests/test_worker_code_pin.py", self.gate)


class VercelGateTests(unittest.TestCase):
    def setUp(self):
        self.deploy = text(WORKFLOWS / "vercel_code_deploy.yml")

    def test_deploy_only_after_green_real_gate_on_main_push(self):
        on = self.deploy.split("permissions:", 1)[0]
        self.assertIn("workflow_run:", on)
        self.assertNotRegex(on, r"(?m)^\s+push:", "direct push trigger would bypass the test gate")
        self.assertIn("- Real bridge unit tests", on)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.deploy)
        self.assertIn("github.event.workflow_run.event == 'push'", self.deploy)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", self.deploy)
        self.assertIn("ref: ${{ github.event.workflow_run.head_sha }}", self.deploy)
        self.assertIn("worker_code_pin same-code", self.deploy)

    def test_failed_gate_cannot_deploy(self):
        condition = self.deploy.split("if: >-", 1)[1].split("runs-on:", 1)[0]
        self.assertIn("&&", condition)
        self.assertNotIn("||", condition)


class WorkerPinningTests(unittest.TestCase):
    def test_scanner_pins_approved_code_before_install_and_run(self):
        workflow = text(WORKFLOWS / "scanner_v18.yml")
        pin_at = workflow.index("python -m tools.worker_code_pin pin")
        self.assertLess(pin_at, workflow.index("pip install -r requirements.txt"))
        self.assertLess(pin_at, workflow.index("bash run_scanner_v18.sh"))
        self.assertIn("fetch-depth: 0", workflow)
        self.assertNotRegex(workflow, r"git checkout HEAD --")

    def test_running_worker_has_no_code_mutating_git_command(self):
        for path in ("run_scanner_v18.sh", "real_trader.py", "real_paper_bridge.py",
                     "real_swing_sell_executor.py", "tools/state_checkpoint.py"):
            code = "\n".join(line for line in text(ROOT / path).splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertIsNone(CODE_MUTATING_GIT.search(code), path)
            self.assertNotRegex(code, r"\[\s*\"git\"\s*,\s*\"(rebase|pull|reset|checkout|stash)\"", path)

    def test_checkpoint_publishes_state_through_state_only_writer(self):
        runner = text(ROOT / "run_scanner_v18.sh")
        self.assertIn("python -m tools.state_checkpoint", runner)
        self.assertIn("python -m tools.worker_code_pin status", runner)
        self.assertIn("readonly WORKER_CODE_SHA", runner)
        self.assertNotIn("git push", runner)
        self.assertNotIn("git commit", runner)
        self.assertIn("state_checkpoint.push_state", text(ROOT / "real_trader.py"))

    def test_watchdog_rolls_out_only_approved_code(self):
        watchdog = text(WORKFLOWS / "scanner_watchdog.yml")
        self.assertIn("- Real bridge unit tests", watchdog)
        self.assertIn("python -m tools.worker_code_pin watchdog", watchdog)
        self.assertIn('code_decision" = "replace"', watchdog)
        self.assertNotIn("current_sources", watchdog)


class TradingLogicUnchangedTests(unittest.TestCase):
    def test_swing_sell_v2_policy_constants(self):
        import swing_sell_v2 as policy
        from decimal import Decimal
        self.assertEqual(policy.LOSS_EXIT_PNL, Decimal("-0.50"))
        self.assertEqual(policy.PARTIAL_TRIGGER_PNL, Decimal("0.25"))
        self.assertEqual(policy.PARTIAL_FRACTION, Decimal("0.20"))
        self.assertEqual(policy.HARD_TP_PNL, Decimal("0.50"))
        self.assertEqual(policy.TRAILING_DISTANCE, Decimal("0.15"))
        self.assertEqual(policy.decide({}, "0.10", "1")["action"], "HOLD")
        self.assertEqual(policy.decide({}, "-0.50", "1")["action"], "FULL_SELL")
        self.assertEqual(policy.decide({}, "0.25", "1")["action"], "PARTIAL_SELL")
        self.assertEqual(policy.decide({}, "0.50", "1")["action"], "FULL_SELL")

    def test_position_and_capital_limits_unchanged(self):
        workflow = text(WORKFLOWS / "scanner_v18.yml")
        for line in ('REAL_MAX_OPEN_POSITIONS: "10"', 'REAL_TRADING_CAPITAL_SDA: "200"',
                     'REAL_POSITION_MAX_SDA: "20"', 'SDA_PAPER_TP4: "1"',
                     'SDA_PAPER_BUY_MODE: "temporal_recovery"',
                     'REAL_TRADING_ENABLED: "true"', 'REAL_TRADING_DRY_RUN: "false"'):
            self.assertIn(line, workflow)
        self.assertIn("MAX_OPEN=10 if PAPER_TP4 else 4", text(ROOT / "v30_pump_hunter.py"))
        self.assertIn('"tp4_single_quote_swing_v11"', text(ROOT / "tools/temporal_recovery_buy.py"))


if __name__ == "__main__":
    unittest.main()
