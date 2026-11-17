"""Regression tests for independent REAL BUY admission; no RPC or wallet actions."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import real_buy_pipeline as pipeline


TOKEN = "0x" + "a" * 40


def cfg():
    return SimpleNamespace(
        position_sda=lambda: 30.0,
        MAX_OPEN_POSITIONS=10,
        TRADING_WALLET_CAPITAL_SDA=300.0,
    )


def real_state(positions=0, pending=0, deployed=0.0, pnl=0.0):
    return {
        "positions": {f"0x{i:040x}": {"amount_sda": 30} for i in range(positions)},
        "pending_buys": {f"0x{i + 100:040x}": {"position_sda": 30} for i in range(pending)},
        "trades": [],
        "daily_realized_pnl_sda": pnl,
        "_test_deployed": deployed,
    }


def safety(amount, state):
    if len(state.get("positions", {})) + len(state.get("pending_buys", {})) >= 10:
        raise RuntimeError("Maximum real open positions reached")
    deployed = state.get("_test_deployed", 0.0)
    if deployed + amount > 300:
        raise RuntimeError("Real trading capital cap of 300 SDA would be exceeded")


def candidate(now):
    return {
        "address": TOKEN, "contract": TOKEN, "symbol": "TEST",
        "score": 75, "quality": 70, "impulse": 8, "phase": "CONFIRMATION",
        "price_sda": 0.001, "quote_at": now.isoformat(), "data_age_sec": 5,
        "m1h": 4, "m15": 2, "volume_1h": 1200, "flow_1h": 450,
        "flow_15m": 100, "trades_1h": 20, "buy_ratio": 1.6,
        "strategy_version": "V30.17",
    }


READY = {"ready": True, "reason": "TWO_FRESH_QUOTES_CONFIRMED",
         "lane": "confirmed_continuation", "sparse_activity_verified": False}


class RealBuyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.signal_state = pipeline.empty_state()
        self.cfg = cfg()

    def test_01_paper_full_real_seven_of_ten_is_eligible(self):
        state = real_state(positions=7)
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          state, self.cfg, safety, self.now)
        self.assertTrue(result["ready"])

    def test_02_paper_cooldown_does_not_block_real(self):
        state = real_state(positions=7)
        # Deliberately no PAPER cooldown input exists in this adapter.
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          state, self.cfg, safety, self.now)
        self.assertTrue(result["ready"])

    def test_03_paper_token_owned_does_not_block_real(self):
        state = real_state(positions=7)
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          state, self.cfg, safety, self.now)
        self.assertTrue(result["ready"])

    def test_04_paper_loss_circuit_does_not_block_real(self):
        state = real_state(positions=7)
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          state, self.cfg, safety, self.now)
        self.assertTrue(result["ready"])

    def test_05_real_cooldown_blocks(self):
        self.signal_state["cooldowns"][TOKEN] = (self.now + timedelta(hours=1)).isoformat()
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          real_state(positions=7), self.cfg, safety, self.now)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "REAL_COOLDOWN")

    def test_06_real_capacity_ten_blocks(self):
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          real_state(positions=10), self.cfg, safety, self.now)
        self.assertFalse(result["ready"])
        self.assertIn("Maximum real open positions", result["reason"])

    def test_07_real_insufficient_capital_blocks(self):
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          real_state(positions=7, deployed=280), self.cfg,
                                          safety, self.now)
        self.assertFalse(result["ready"])
        self.assertIn("capital cap", result["reason"])

    def test_08_admission_has_no_paper_file_dependencies(self):
        source = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertNotIn("positions.json", source)
        self.assertNotIn("forward_shadow_state.json", source)
        result = pipeline.admit_candidate(candidate(self.now), READY, self.signal_state,
                                          real_state(positions=7), self.cfg, safety, self.now)
        self.assertTrue(result["ready"])

    def test_09_restart_preserves_temporal_state_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real_buy_signal_state.json"
            signal_id = "a" * 64
            self.signal_state["signals"][TOKEN] = {
                "phase": "CONFIRMATION", "quote_at": self.now.isoformat(),
                "decision_at": self.now.isoformat(), "data_age_sec": 5,
                "score": 75, "quality": 70, "impulse": 8,
            }
            self.signal_state["consumed_signal_ids"] = [signal_id]
            pipeline.save_state(self.signal_state, path)
            restored = pipeline.load_state(path)
            self.assertEqual(restored["signals"][TOKEN]["quote_at"], self.now.isoformat())
            self.assertEqual(restored["consumed_signal_ids"], [signal_id])
            c = candidate(self.now)
            # Same signal identity is blocked after restart.
            c["strategy_version"] = "V30.17"
            result = pipeline.admit_candidate(c, READY, restored, real_state(positions=7),
                                              self.cfg, safety, self.now)
            # The exact id is deterministic; seed it using the same derivation.
            import hashlib
            expected = hashlib.sha256(
                f"{TOKEN}|{c['quote_at']}|{c['strategy_version']}|REAL".encode()
            ).hexdigest()
            restored["consumed_signal_ids"] = [expected]
            result = pipeline.admit_candidate(c, READY, restored, real_state(positions=7),
                                              self.cfg, safety, self.now)
            self.assertFalse(result["ready"])
            self.assertEqual(result["reason"], "REAL_DUPLICATE_SIGNAL")

    def test_10_dry_run_reports_would_execute_without_calling_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.json"
            state_path = Path(tmp) / "state.json"
            market_path.write_text(json.dumps({
                "schema_version": 1, "updated_at": self.now.isoformat(),
                "market_source_at": self.now.isoformat(), "candidates": [candidate(self.now)],
            }), encoding="utf-8")
            fake_cfg = SimpleNamespace(
                REAL_TRADING_ENABLED=True, REAL_TRADING_DRY_RUN=True,
                TRADING_WALLET_CAPITAL_SDA=300.0, MAX_OPEN_POSITIONS=10,
                position_sda=lambda: 30.0,
            )
            fake_trader = SimpleNamespace(
                load_state=Mock(return_value=real_state(positions=7)),
                safety_check=Mock(),
                execute_buy=Mock(side_effect=AssertionError("must not execute in DRY RUN")),
                deployed_cost_basis=Mock(return_value=210.0),
            )
            with patch.dict("sys.modules", {
                "real_trading_config": fake_cfg,
                "real_trader": fake_trader,
            }), patch.object(pipeline, "_candidate_signal",
                             return_value=({}, dict(READY))):
                result = pipeline.run(market_path, state_path, at=self.now)
            self.assertEqual(result["actions"][0]["status"], "WOULD_EXECUTE")
            fake_trader.safety_check.assert_called()
            fake_trader.execute_buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
