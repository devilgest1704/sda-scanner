import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import v30_pump_hunter as v30


class PaperTelemetryTests(unittest.TestCase):
    def test_entry_samples_and_persisted_exit(self):
        def original_create(address, analysis, signal, meta, liq, investment=None):
            return {
                "address": address, "label": "TEST/SDA",
                "entry_price": 100.0, "opened_at": datetime.now(timezone.utc).isoformat(),
                "investment_sda": 50.0, "remaining_fraction": 1.0,
            }

        def close(state, address, price, reason):
            pos = state["positions"].pop(address)
            result = {**pos, "address": address, "close_reason": reason}
            state["closed_trades"].append(result)
            return result

        engine = SimpleNamespace(create=original_create, close=close, main=lambda: None)
        main_module = SimpleNamespace()
        signal = {
            "pump_score": 70, "pump_quality": 60, "pump_change": 10,
            "pump_phase": "IGNITION", "m1h": 2, "m15": 1,
            "m4h": 1, "net_1h": 200, "volume_1h": 700,
            "trades_1h": 12, "data_age_sec": 60,
        }
        with patch.object(v30, "decision", return_value=signal), patch.object(v30, "save_state"), patch.object(v30, "load_state", return_value={"cooldowns": {}}):
            v30.patch(main_module, engine)
            pos = engine.create("0xtest", {"price_in_sda": 100}, signal, {}, {})
            self.assertEqual(pos["pump_entry_snapshot"]["phase"], "IGNITION")
            state = {"positions": {"0xtest": pos}, "closed_trades": []}
            engine._auto_exit(state, {"0xtest": {"analysis": {"price_in_sda": 101}}}, {})
            self.assertEqual(len(pos["pump_price_samples"]), 1)
            self.assertGreater(pos["pump_mfe_pct"], 0)
            engine._auto_exit(state, {"0xtest": {"analysis": {"price_in_sda": 90}}}, {})
            self.assertEqual(len(state["closed_trades"]), 1)
            closed = state["closed_trades"][0]
            self.assertEqual(len(closed["pump_price_samples"]), 2)
            self.assertLess(closed["pump_mae_pct"], 0)
            self.assertIn("pump_exit_observed_at", closed)
            self.assertIn("pump_exit_snapshot", closed)
            self.assertEqual(closed["pump_exit_snapshot"]["price_sda"], 90)

    def test_buy_gate_diagnostics_include_impulse_without_changing_decision(self):
        metrics = {"price": 1.0, "m1": 2.94, "m15": 2.96, "m4": 3.92,
                   "flow": 3274.0, "flow15": 2100.0, "vol": 5322.0,
                   "trades": 29, "buy_ratio": 2.0, "trade_ratio": 2.0,
                   "data_age_sec": 150.0}
        config = {"entry_score": 68.0, "quality": 52.0, "impulse": 8.0,
                  "buy_ratio": 1.10, "trades": 5}
        history = {"0xtest": {"last": dict(metrics), "prev": dict(metrics), "fresh": True}}
        with patch.object(v30, "score", return_value=(metrics, 69.9, 68.2, 1.7, "IGNITION")), \
             patch.object(v30, "cooldown_active", return_value=False), \
             patch.object(v30, "load_agent_config", return_value=config), \
             patch.object(v30, "load_state", return_value={"history": history}):
            result = v30.decision("0xtest", {}, persist=False)
        self.assertTrue(result["paper_buy_blocked"])
        self.assertFalse(result["eligible_for_buy"])
        self.assertIn("impulse", result["paper_buy_gate_failures"])
        self.assertNotIn("score", result["paper_buy_gate_failures"])

if __name__ == "__main__":
    unittest.main()
