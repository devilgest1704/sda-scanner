"""REAL sizing: fixed 30 SDA BUY, 300 SDA remaining-cost-basis cap, 10 positions.

All chain access is the in-memory FakeChain or pure state; no RPC, signing or broadcast.
"""
import copy
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import real_paper_bridge as bridge
import real_swing_sell_executor as executor
import real_trader
import real_trading_config as cfg
import swing_sell_v2 as policy
from tests.test_real_sell_p0_fixes import NEW_TOKEN, TOKEN, BridgeHarness, FakeChain


def _state(costs, pending=None):
    return {
        "positions": {"0x" + f"{i + 1:040x}": {"remaining_cost_basis": str(cost), "amount_sda": float(cost)}
                      for i, cost in enumerate(costs)},
        "pending_buys": pending or {},
        "daily_pnl_date": datetime.now(timezone.utc).date().isoformat(),
        "daily_realized_pnl_sda": 0.0,
    }


class SizingConfiguration(unittest.TestCase):
    def test_production_defaults(self):
        self.assertEqual(cfg.position_sda(), 30.0)
        self.assertEqual(cfg.TRADING_WALLET_CAPITAL_SDA, 300.0)
        self.assertEqual(cfg.MAX_OPEN_POSITIONS, 10)
        self.assertEqual(cfg.MAX_OPEN_POSITIONS * cfg.position_sda(), cfg.TRADING_WALLET_CAPITAL_SDA)

    def test_sell_v2_rules_unchanged(self):
        self.assertEqual((policy.LOSS_EXIT_PNL, policy.PARTIAL_TRIGGER_PNL, policy.PARTIAL_FRACTION,
                          policy.TRAILING_DISTANCE, policy.HARD_TP_PNL),
                         (Decimal("-0.50"), Decimal("0.25"), Decimal("0.20"), Decimal("0.15"), Decimal("0.50")))


class CapitalAdmission(unittest.TestCase):
    def test_deployed_240_plus_30_allowed(self):
        real_trader.safety_check(30, _state([30] * 8))

    def test_deployed_270_plus_30_allowed(self):
        real_trader.safety_check(30, _state([30] * 9))

    def test_deployed_280_plus_30_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "capital cap of 300 SDA"):
            real_trader.safety_check(30, _state([35] * 8))

    def test_deployed_300_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "capital cap"):
            real_trader.safety_check(30, _state([50] * 6))

    def test_ten_positions_blocked_regardless_of_spare_capital(self):
        with self.assertRaisesRegex(RuntimeError, "Maximum real open positions"):
            real_trader.safety_check(30, _state([1] * 10))

    def test_pending_buy_reserves_slot_and_capital(self):
        with self.assertRaisesRegex(RuntimeError, "capital cap"):
            real_trader.safety_check(30, _state([30] * 8, {"0x" + "f" * 40: {"position_sda": 40.0}}))

    def test_mixed_legacy_20_sda_portfolio_uses_actual_basis_and_is_not_mutated(self):
        state = _state([20] * 8 + ["19.99999999999999981654386984"])
        before = copy.deepcopy(state)
        self.assertLess(real_trader.deployed_cost_basis(state), Decimal(180))
        real_trader.safety_check(30, state)
        self.assertEqual(state, before)

    def test_remaining_cost_basis_is_preferred_over_stale_amount(self):
        state = _state([])
        state["positions"]["0x" + "a" * 40] = {"amount_sda": 30.0, "remaining_cost_basis": "24"}
        state["positions"]["0x" + "b" * 40] = {"amount_sda": 20.0}
        self.assertEqual(real_trader.deployed_cost_basis(state), Decimal(44))

    def test_open_position_is_never_topped_up(self):
        state = _state([20])
        token = next(iter(state["positions"]))
        with patch.object(real_trader, "load_state", return_value=state), \
                patch.object(real_trader, "prepare_buy", side_effect=AssertionError("top-up")):
            self.assertEqual(real_trader.execute_buy(token, "OLD", 30)["status"], "ALREADY_OPEN")
        self.assertEqual(state["positions"][token]["remaining_cost_basis"], "20")


class CapitalRelease(unittest.TestCase):
    def test_partial_sell_releases_only_allocated_basis(self):
        chain = FakeChain(self, quantity=100, cost="30")
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 20, 9 * 10**18),
                           "unwrap": lambda tx: chain.mine_plain(tx)}
        executor.evaluate_position(TOKEN, "TEST", "0.38", chain.clock.isoformat())  # +26.7%
        self.assertEqual(chain.position()["remaining_quantity"], 80)
        self.assertEqual(real_trader.deployed_cost_basis(chain.state), Decimal(24))
        self.assertEqual(chain.sent_kinds(), ["swap", "unwrap"])

    def test_full_sell_releases_remaining_basis(self):
        chain = FakeChain(self, quantity=100, cost="30")
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 100, 14 * 10**18),
                           "unwrap": lambda tx: chain.mine_plain(tx)}
        executor.evaluate_position(TOKEN, "TEST", "0.15", chain.clock.isoformat())  # -50%
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(real_trader.deployed_cost_basis(chain.state), Decimal(0))

    def test_manual_sell_releases_basis_through_reconciliation(self):
        chain = FakeChain(self, quantity=100, cost="30")
        chain.balances[TOKEN] = 70
        executor.reconcile_position(TOKEN)
        self.assertAlmostEqual(float(real_trader.deployed_cost_basis(chain.state)), 21.0)
        self.assertEqual(chain.sent, [])

    def test_dust_consumes_no_meaningful_capital(self):
        chain = FakeChain(self, quantity=30 * 10**18, cost="30")
        with patch.object(real_trader, "token_decimals", return_value=18):
            chain.balances[TOKEN] = 5 * 10**13
            executor.reconcile_position(TOKEN)
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(real_trader.deployed_cost_basis(chain.state), Decimal(0))
        self.assertEqual(chain.sent, [])


class FreshSignalsOnly(unittest.TestCase):
    def test_skipped_signal_not_replayed_and_fresh_signal_uses_30_sda(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.buy.side_effect = RuntimeError("Real trading capital cap of 300 SDA would be exceeded")
        harness.paper.write_text(json.dumps({"positions": {NEW_TOKEN: {
            "symbol": "OLD", "investment_sda": 20, "opened_at": bridge._now()}}}), encoding="utf-8")
        first = harness.run()
        self.assertIn("PAPER_BUY_SKIPPED_REAL_LIMIT", json.dumps(first["actions"]))
        harness.buy.side_effect = None
        harness.buy.reset_mock()
        harness.run()  # capital is free again; the old signal must not be replayed
        harness.buy.assert_not_called()
        fresh = "0x" + "f" * 40
        harness.paper.write_text(json.dumps({"positions": {
            NEW_TOKEN: {"symbol": "OLD", "investment_sda": 20, "opened_at": bridge._now()},
            fresh: {"symbol": "FRESH", "investment_sda": 20, "opened_at": bridge._now()}}}), encoding="utf-8")
        harness.run()
        harness.buy.assert_called_once_with(fresh, "FRESH", 30.0)
        self.assertEqual(chain.sent, [])
