"""MAX HOLD policy and mock-chain execution; tests cannot reach live RPC."""
from datetime import timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import real_max_hold as rule
import real_swing_sell_executor as executor
import real_trader as trader
from test_real_sell_p0_fixes import FakeChain, TOKEN

SAVE_STATE = trader.save_state
LOAD_STATE = trader.load_state


class MaxHoldTests(TestCase):
    def setUp(self):
        self.chain = FakeChain(self)
        self.position = self.chain.position()
        self.position["opened_at"] = (self.chain.clock - timedelta(hours=100)).isoformat()
        self.mark = "0.21"
        self.quote = 21 * 10**18
        self.snapshot = self.market_snapshot()
        self.start_patch(executor, "time", side_effect=lambda: self.chain.clock.timestamp())
        self.start_patch(executor.market_prices, "load_market_snapshot", return_value=self.snapshot)
        self.start_patch(trader, "gas_price", return_value=10**9)
        self.start_patch(trader, "native_balance", return_value=10**18)
        self.start_patch(trader, "estimate_gas", return_value=100000)
        self.quote_call = self.start_patch(trader, "quote_best_live_fee", side_effect=self.executable_quote)
        self.history()

    def start_patch(self, module, name, **kwargs):
        patcher = patch.object(module, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def market_snapshot(self):
        stamp = self.chain.clock.isoformat()
        return {"updated_at": stamp, "tokens": {TOKEN: {"last_transaction": stamp,
                "momentum": {"15m_pct": 0, "1h_pct": 0, "4h_pct": 0},
                "flow": {key: {"net_flow": 0, "transactions": 5} for key in ("15m", "1h", "4h")}}}}

    def executable_quote(self, _in, _out, _token, _owner, amount, value_wei=0):
        self.assertEqual((amount, value_wei), (100, 0))
        return 3000, self.quote, "0x", int(self.chain.clock.timestamp()) + 120

    def history(self, starting="4.7"):
        price = Decimal("0.2") * (1 + Decimal(starting) / 100)
        start = self.chain.clock.timestamp() - rule.STAGNATION_WINDOW
        self.position[rule.HISTORY_KEY] = [[start + index * 900, str(price), starting] for index in range(96)]

    def evaluate(self, stamp=None):
        return executor.evaluate_position(TOKEN, "TEST", self.mark, stamp or self.chain.clock.isoformat())

    def shadow(self):
        return self.chain.position()[rule.SHADOW_KEY][-1]

    def assert_hold(self, blocker):
        result = self.evaluate()
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(self.shadow()["blocker"], blocker)
        self.assertEqual(self.shadow()["decision"], "SHADOW_MAX_HOLD_HOLD")
        self.assertFalse(self.chain.sent)
        self.assertFalse(self.chain.state["pending_sells"])

    def test_manual_stagnant_profit_full_sell_and_slot_release(self):
        self.chain.auto_mine["swap"] = lambda tx: self.chain.mine_swap(tx, 100, self.quote)
        self.chain.auto_mine["unwrap"] = self.chain.mine_plain
        self.assertEqual(len(self.chain.state["positions"]), 1)
        result = self.evaluate()
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(self.chain.sent_kinds(), ["swap", "unwrap"])
        self.assertEqual(self.chain.trades("SELL")[0]["sell_reason"], rule.REASON)
        self.assertFalse(self.chain.state["positions"])
        self.assertEqual(trader.deployed_cost_basis(self.chain.state), 0)
        self.assertFalse(self.chain.state["pending_sells"])

    def test_auto_small_positive_net_passes_without_treating_slippage_as_cost(self):
        self.position.update(source="AUTO")
        self.mark = "0.201"
        self.quote = 20 * 10**18 + 10**17
        self.history("0.2")
        self.evaluate()
        shadow = self.shadow()
        self.assertEqual(self.chain.sent_kinds(), ["swap"])
        self.assertEqual(shadow["decision"], "SHADOW_MAX_HOLD_STAGNANT_PROFIT")
        self.assertEqual(shadow["source"], "AUTO")
        self.assertEqual(Decimal(shadow["improvement_pp"]), Decimal("0.3"))
        self.assertGreater(shadow["executable_sell_quote"]["minimum_wsda_raw"], 20 * 10**18)

    def test_nonpositive_net_exit_holds(self):
        self.quote = 20 * 10**18 + 1
        self.assert_hold("NET_EXIT_NOT_PROFITABLE")

    def test_loss_never_passes_max_hold(self):
        self.mark = "0.198"
        self.position["opened_at"] = (self.chain.clock - timedelta(hours=300)).isoformat()
        self.assert_hold("NON_POSITIVE_PNL")

    def test_48_hours_holds(self):
        self.position["opened_at"] = (self.chain.clock - timedelta(hours=48)).isoformat()
        self.assert_hold("MIN_HOLD_NOT_MET")

    def test_window_peak_not_latest_pnl_controls_improvement(self):
        self.position[rule.HISTORY_KEY][40][2] = "6.7"
        self.assert_hold("PNL_IMPROVING")
        self.assertEqual(Decimal(self.shadow()["improvement_pp"]), 2)

    def test_bullish_4h_holds(self):
        self.snapshot["tokens"][TOKEN]["momentum"]["4h_pct"] = 2
        self.assert_hold("POSITIVE_MOMENTUM")

    def test_stale_trend_holds(self):
        self.snapshot["updated_at"] = (self.chain.clock - timedelta(seconds=301)).isoformat()
        self.assert_hold("STALE_TREND")

    def test_incomplete_flow_holds(self):
        del self.snapshot["tokens"][TOKEN]["flow"]["4h"]
        self.assert_hold("INSUFFICIENT_TREND")

    def test_stale_price_is_not_recorded(self):
        self.evaluate((self.chain.clock - timedelta(seconds=301)).isoformat())
        self.assertEqual(self.shadow()["blocker"], "STALE_PRICE")
        self.assertEqual(len(self.chain.position()[rule.HISTORY_KEY]), 96)
        self.quote_call.assert_not_called()

    def test_partial_done_disables_rule(self):
        self.position["profit_partial_done"] = True
        self.assert_hold("PARTIAL_ALREADY_DONE")

    def test_trailing_armed_disables_rule(self):
        self.position["trailing_armed"] = True
        self.assert_hold("TRAILING_ARMED")

    def test_restart_recovers_real_history_from_disk(self):
        self.snapshot["updated_at"] = None
        self.evaluate()
        SAVE_STATE(self.chain.state)
        self.chain.state = LOAD_STATE()
        self.snapshot.update(self.market_snapshot())
        self.evaluate()
        self.assertEqual(self.chain.sent_kinds(), ["swap"])
        self.assertEqual(self.shadow()["history_coverage"], 1)

    def test_pending_sell_cannot_create_second_intent(self):
        self.evaluate()
        first = self.chain.state["pending_sells"][TOKEN]["sell_intent_id"]
        self.evaluate()
        self.assertEqual(self.chain.sent_kinds(), ["swap"])
        self.assertEqual(self.chain.state["pending_sells"][TOKEN]["sell_intent_id"], first)

    def test_sparse_history_and_missing_allowance_fail_closed(self):
        self.position[rule.HISTORY_KEY] = [self.position[rule.HISTORY_KEY][0]]
        self.assert_hold("INSUFFICIENT_HISTORY")
        self.position = self.chain.position()
        self.history()
        self.chain.allowance_value = 0
        self.assert_hold("EXECUTABLE_QUOTE_REQUIRES_ALLOWANCE")
        self.quote_call.assert_not_called()

    def test_quote_rechecked_before_swap(self):
        allowed = self.executable_quote(None, None, TOKEN, None, 100)
        self.quote = 20 * 10**18
        denied = self.executable_quote(None, None, TOKEN, None, 100)
        self.quote_call.side_effect = [allowed, denied]
        result = self.evaluate()
        self.assertEqual(result["status"], "NET_EXIT_NOT_PROFITABLE")
        self.assertFalse(self.chain.sent)
        self.assertFalse(self.chain.state["pending_sells"])