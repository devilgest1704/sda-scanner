"""P0 REAL SELL regressions: double counting, lifecycle, backoff, quarantine, health.

All chain access is an in-memory fake; no RPC, signing or broadcast happens.
"""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import real_market_price as market_prices
import real_paper_bridge as bridge
import real_swing_sell_executor as executor
import real_trader
import real_trading_config as cfg
import swing_sell_v2 as policy

TOKEN = "0x" + "a" * 40
WSDA = "0x" + "b" * 40
WALLET = "0x" + "c" * 40
ROUTER = "0x" + "d" * 40
NEW_TOKEN = "0x" + "e" * 40


def _topic(address):
    return "0x" + address[2:].rjust(64, "0")


class FakeChain:
    """Deterministic chain/wallet double used by every test in this module."""

    def __init__(self, test, quantity=100, cost="20"):
        self.clock = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.state = {
            "sell_strategy_version": policy.STRATEGY_VERSION,
            "positions": {TOKEN: {
                "token": TOKEN, "symbol": "TEST", "buy_tx": "0xbuy", "position_id": "0xbuy",
                "token_amount_raw": quantity, "amount_sda": float(cost),
                "opened_at": "2026-11-09T00:00:00+00:00",
            }},
            "pending_buys": {}, "pending_sells": {}, "sell_intents": {},
            "sell_intent_counters": {}, "trades": [], "daily_realized_pnl_sda": 0.0,
        }
        self.balances = {TOKEN: quantity, WSDA: 0}
        self.receipts = {}
        self.mempool = set()
        self.latest_nonce = 0
        self.sent = []
        self.quote_error = None
        self.broadcast_error = None
        self.auto_mine = {}
        self.allowance_value = 10**30
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.patches = [
            patch.object(real_trader, "load_state", side_effect=lambda: copy.deepcopy(self.state)),
            patch.object(real_trader, "save_state", side_effect=self._save),
            patch.object(real_trader, "now", side_effect=lambda: self.clock.isoformat()),
            patch.object(real_trader, "wallet_address", return_value=WALLET),
            patch.object(real_trader, "private_account", return_value=SimpleNamespace(address=WALLET)),
            patch.object(real_trader, "chain_id", return_value=999),
            patch.object(real_trader, "token_decimals", return_value=0),
            patch.object(real_trader, "token_balance", side_effect=lambda token, owner: self.balances.get(real_trader.addr(token), 0)),
            patch.object(real_trader, "allowance", side_effect=lambda *a: self.allowance_value),
            patch.object(real_trader, "quote_best_live_fee", side_effect=self._quote),
            patch.object(real_trader, "min_out", side_effect=lambda value: int(value * 0.99)),
            patch.object(real_trader, "build_exact_input_single", return_value="0xswapdata"),
            patch.object(real_trader, "send_transaction", side_effect=self._send),
            patch.object(real_trader, "rpc", side_effect=self._rpc),
            patch.object(real_trader, "persist_real_intent_before_broadcast", side_effect=AssertionError("real persist")),
            patch.object(cfg, "REAL_TRADING_ENABLED", True),
            patch.object(cfg, "REAL_TRADING_DRY_RUN", False),
            patch.object(cfg, "STATE_FILE", str(Path(self.tmp.name) / "real_trade_state.json")),
            patch.object(cfg, "WSDA_ADDRESS", WSDA),
            patch.object(cfg, "ROUTER_ADDRESS", ROUTER),
            patch.object(cfg, "CHAIN_ID", 999),
            patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_REPOSITORY": ""}),
        ]
        for item in self.patches:
            item.start()
            test.addCleanup(item.stop)

    def _save(self, value):
        self.state = copy.deepcopy(value)

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def _quote(self, token_in, token_out, token, owner, amount, value_wei=0):
        if self.quote_error:
            raise RuntimeError(self.quote_error)
        return 3000, int(amount) * 25 * 10**16, "0x", int(self.clock.timestamp()) + 120

    def _send(self, account, to, data, value=0, gas_limit=None, before_broadcast=None):
        kind = "approval" if str(data).startswith(cfg.APPROVE_SELECTOR) else (
            "swap" if data == "0xswapdata" else "unwrap")
        tx = f"0x{kind}{len(self.sent)}"
        nonce = len([s for s in self.sent if s[2]])
        if before_broadcast:
            before_broadcast({"tx_hash": tx, "nonce": nonce, "gas": 100000, "gas_price": 1})
        if self.broadcast_error:
            self.sent.append((kind, tx, False))
            raise RuntimeError(self.broadcast_error)
        self.sent.append((kind, tx, True))
        self.mempool.add(tx)
        mined = self.auto_mine.get(kind)
        if mined:
            mined(tx)
        return tx, 100000, 1

    def mine_swap(self, tx, sold, proceeds):
        self.mempool.discard(tx)
        self.balances[TOKEN] -= sold
        self.balances[WSDA] += proceeds
        self.latest_nonce += 1
        self.receipts[tx] = {"status": "0x1", "logs": [
            {"address": TOKEN, "topics": [executor.TRANSFER_TOPIC, _topic(WALLET), _topic(ROUTER)],
             "data": hex(sold)},
            {"address": WSDA, "topics": [executor.TRANSFER_TOPIC, _topic(ROUTER), _topic(WALLET)],
             "data": hex(proceeds)},
        ]}

    def mine_plain(self, tx, ok=True):
        self.mempool.discard(tx)
        self.latest_nonce += 1
        self.receipts[tx] = {"status": "0x1" if ok else "0x0", "logs": []}

    def _rpc(self, method, params):
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            return {"hash": params[0]} if params[0] in self.mempool else None
        if method == "eth_getTransactionCount":
            return hex(self.latest_nonce)
        raise AssertionError(f"unexpected rpc {method}")

    def sent_kinds(self):
        return [kind for kind, _tx, _ok in self.sent]

    def trades(self, action):
        return [t for t in self.state["trades"] if t.get("action") == action]

    def position(self):
        return self.state["positions"].get(TOKEN)


class BridgeHarness:
    """Runs the real production bridge with fake chain, PAPER and market inputs."""

    def __init__(self, test, chain):
        self.chain = chain
        self.test = test
        root = Path(chain.tmp.name)
        self.paper = root / "positions.json"
        self.paper.write_text(json.dumps({"positions": {}}), encoding="utf-8")
        (root / "bridge_state.json").write_text(json.dumps({
            "initialized": True, "paper_positions": [], "pending_buy": [], "pending_sell": [],
        }), encoding="utf-8")
        self.price = Decimal("0.2")
        self.buy = unittest.mock.Mock(return_value={"status": "CONFIRMED", "tx": "0xnewbuy"})
        for item in (
            patch.object(bridge, "PAPER_FILE", self.paper),
            patch.object(bridge, "STATE_FILE", root / "bridge_state.json"),
            patch.object(bridge, "EVENTS_FILE", root / "bridge_events.json"),
            patch.object(market_prices, "load_market_snapshot", side_effect=self._snapshot),
            patch.object(market_prices, "load_token_metadata", return_value={}),
            patch.object(market_prices, "newest_supabase_price", return_value=None),
            patch.object(real_trader, "execute_buy", self.buy),
            patch.dict(os.environ, {"REAL_TRADING_ENABLED": "true", "REAL_TRADING_DRY_RUN": "false",
                                    "SDA_PAPER_BUY_MODE": "temporal_recovery", "SDA_PAPER_TP4": "1"}),
        ):
            item.start()
            test.addCleanup(item.stop)

    def _snapshot(self):
        stamp = datetime.now(timezone.utc).isoformat()
        return {"updated_at": stamp, "tokens": {TOKEN: {"price_in_sda": str(self.price), "last_transaction": stamp}}}

    def run(self):
        return bridge.reconcile()


class PartialDoubleCountRegression(unittest.TestCase):
    def test_partial_swap_confirmed_unwrap_pending_is_not_manual_sell(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.25")  # +25% versus 20 SDA / 100 tokens
        first = harness.run()
        self.assertEqual(chain.sent_kinds(), ["swap"])
        swap_tx = chain.sent[0][1]
        self.assertEqual(chain.state["pending_sells"][TOKEN]["stage"], "SWAP_SUBMITTED")
        self.assertEqual(first["sell_health"], "SELL_PENDING")

        chain.mine_swap(swap_tx, 20, 5 * 10**18)  # wallet now holds 80
        chain.advance(60)
        second = harness.run()  # swap confirmed, unwrap broadcast but not mined
        self.assertEqual(chain.sent_kinds(), ["swap", "unwrap"])
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        pos = chain.position()
        self.assertEqual(pos["remaining_quantity"], 80)
        self.assertEqual(Decimal(pos["remaining_cost_basis"]), Decimal("16"))
        self.assertNotIn("MANUAL_RECONCILED", json.dumps(second["actions"]))

        chain.mine_plain(chain.sent[1][1])
        chain.advance(60)
        third = harness.run()
        pos = chain.position()
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertEqual(len(chain.trades("SELL")), 1)
        self.assertEqual(pos["remaining_quantity"], 80)
        self.assertEqual(Decimal(pos["remaining_cost_basis"]), Decimal("16"))
        self.assertTrue(pos["profit_partial_done"])
        self.assertTrue(pos["trailing_armed"])
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertIn(third["sell_health"], {"SELL_EXECUTED", "SELL_HOLD"})
        self.assertAlmostEqual(chain.trades("SELL")[0]["realized_pnl_sda"], 1.0)

    def test_swap_mined_after_startup_recovery_is_still_resolved_before_wallet_compare(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.25")
        harness.run()
        swap_tx = chain.sent[0][1]
        chain.mine_swap(swap_tx, 20, 5 * 10**18)
        with patch.object(executor, "startup_recovery", return_value=[]):
            harness.run()
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertEqual(chain.position()["remaining_quantity"], 80)
        self.assertEqual(Decimal(chain.position()["remaining_cost_basis"]), Decimal("16"))

    def test_unresolved_post_swap_intent_blocks_manual_classification(self):
        chain = FakeChain(self)
        chain.state["pending_sells"][TOKEN] = {
            "sell_intent_id": "legacy", "token": TOKEN, "symbol": "TEST", "position_id": "0xbuy",
            "sell_reason": "PROFIT_PARTIAL_25", "stage": "UNWRAP_SUBMITTED", "status": "SUBMITTED",
            "tx_hash": "0xunwrap-legacy", "swap_tx": "0xswap-legacy", "token_balance_before": 100,
            "requested_quantity_raw": 20, "confirmed_sold_quantity_raw": 20,
            "confirmed_proceeds_raw": 5 * 10**18, "created_at": chain.clock.isoformat(),
        }
        chain.balances[TOKEN] = 80
        chain.mempool.add("0xunwrap-legacy")
        result = executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(result["status"], "SELL_INTENT_ACTIVE")
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertEqual(chain.position()["token_amount_raw"], 100)


class FullSellRegression(unittest.TestCase):
    def test_full_sell_closes_once_without_manual_or_position_disappeared(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.1")  # -50%
        harness.run()
        swap_tx = chain.sent[0][1]
        chain.mine_swap(swap_tx, 100, 10 * 10**18)  # wallet becomes zero
        result = harness.run()
        self.assertNotIn("Position disappeared", json.dumps(result))
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertNotIn(TOKEN, chain.state["positions"])
        chain.mine_plain(chain.sent[1][1])
        final = harness.run()
        self.assertNotIn("Position disappeared", json.dumps(final))
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertEqual(len(chain.trades("SELL")), 1)
        self.assertEqual(chain.trades("SELL")[0]["sold_quantity_raw"], 100)
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(chain.state.get("manual_disposals", []), [])
        self.assertEqual(chain.sent_kinds(), ["swap", "unwrap"])


class ManualSellRegression(unittest.TestCase):
    def test_manual_partial_after_automatic_partial_is_reconciled_once(self):
        chain = FakeChain(self)
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 20, 5 * 10**18),
                           "unwrap": lambda tx: chain.mine_plain(tx)}
        executor.evaluate_position(TOKEN, "TEST", "0.25", chain.clock.isoformat())
        self.assertEqual(chain.position()["remaining_quantity"], 80)
        chain.balances[TOKEN] = 60  # user sold 20 manually afterwards
        result = executor.reconcile_position(TOKEN)
        self.assertEqual(result["wallet"]["status"], "MANUAL_RECONCILED")
        self.assertEqual(len(chain.trades("MANUAL_SELL")), 1)
        self.assertEqual(chain.trades("MANUAL_SELL")[0]["sold_quantity_raw"], 20)
        self.assertEqual(chain.position()["remaining_quantity"], 60)
        self.assertAlmostEqual(float(chain.position()["remaining_cost_basis"]), 12.0)

    def test_manual_full_and_partial_still_reconcile(self):
        chain = FakeChain(self)
        chain.balances[TOKEN] = 70
        self.assertEqual(executor.reconcile_position(TOKEN)["wallet"]["status"], "MANUAL_RECONCILED")
        self.assertAlmostEqual(float(chain.position()["remaining_cost_basis"]), 14.0)
        chain.balances[TOKEN] = 0
        self.assertEqual(executor.reconcile_position(TOKEN)["wallet"]["status"], "CLOSED_MANUAL_RECONCILED")
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(chain.sent, [])

    def test_wallet_excess_is_ignored(self):
        chain = FakeChain(self)
        chain.balances[TOKEN] = 150
        result = executor.reconcile_position(TOKEN)
        self.assertEqual(result["wallet"]["status"], "WALLET_EXCESS_IGNORED")
        self.assertEqual(chain.position()["token_amount_raw"], 100)


class DroppedTransactionRecovery(unittest.TestCase):
    def test_broadcast_failure_becomes_dropped_then_controlled_retry(self):
        chain = FakeChain(self)
        chain.broadcast_error = "node rejected raw transaction"
        result = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(result["status"], "UNKNOWN")
        intent = chain.state["pending_sells"][TOKEN]
        self.assertEqual(intent["stage"], "SWAP_SUBMITTED")
        chain.broadcast_error = None

        # During uncertainty: no duplicate transaction and no new intent.
        chain.advance(30)
        pending = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(pending["status"], "PENDING_CONFIRMATION")
        self.assertEqual(len(chain.sent), 1)
        self.assertEqual(chain.state["pending_sells"][TOKEN]["stage"], "SWAP_SUBMITTED")

        # Restart after the on-chain swap deadline: absence is conclusive.
        chain.advance(300)
        recovered = executor.startup_recovery()
        self.assertEqual(recovered[0]["status"], "DROPPED")
        self.assertEqual(chain.state["pending_sells"], {})
        stored = chain.state["sell_intents"][intent["sell_intent_id"]]
        self.assertEqual(stored["stage"], "DROPPED")
        self.assertEqual(chain.position()["token_amount_raw"], 100)

        # Retry only after backoff and only with a new intent.
        backoff = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(backoff["status"], "SELL_BACKOFF")
        self.assertEqual(len(chain.sent), 1)
        chain.advance(61)
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 100, 10 * 10**18),
                           "unwrap": lambda tx: chain.mine_plain(tx)}
        done = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(done["status"], "CONFIRMED")
        self.assertEqual(chain.sent_kinds(), ["swap", "swap", "unwrap"])
        self.assertEqual(len(chain.trades("SELL")), 1)

    def test_swap_absence_with_reduced_wallet_is_not_dropped(self):
        chain = FakeChain(self)
        chain.broadcast_error = "timeout after send"
        executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        chain.broadcast_error = None
        chain.balances[TOKEN] = 0  # tokens already left: absence is not conclusive
        chain.advance(600)
        result = executor.startup_recovery()[0]
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn(TOKEN, chain.state["pending_sells"])
        self.assertEqual(chain.trades("MANUAL_SELL"), [])

    def test_mempool_transaction_stays_pending(self):
        chain = FakeChain(self)
        executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        chain.advance(3600)
        result = executor.startup_recovery()[0]
        self.assertEqual(result["status"], "PENDING_CONFIRMATION")
        self.assertEqual(len(chain.sent), 1)

    def test_rpc_uncertainty_never_drops(self):
        chain = FakeChain(self)
        executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        chain.mempool.clear()
        chain.advance(3600)
        with patch.object(real_trader, "rpc", side_effect=TimeoutError("rpc timeout")):
            result = executor.startup_recovery()[0]
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(chain.state["pending_sells"][TOKEN]["stage"], "SWAP_SUBMITTED")

    def test_dropped_unwrap_retries_unwrap_without_second_swap(self):
        chain = FakeChain(self)
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 100, 10 * 10**18)}
        executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        unwrap_tx = chain.sent[1][1]
        chain.mempool.discard(unwrap_tx)
        chain.latest_nonce += 1  # nonce consumed by some other transaction
        chain.advance(600)
        self.assertEqual(executor.startup_recovery()[0]["status"], "UNWRAP_DROPPED")
        chain.auto_mine = {"unwrap": lambda tx: chain.mine_plain(tx)}
        self.assertEqual(executor.startup_recovery()[0]["status"], "CONFIRMED")
        self.assertEqual(chain.sent_kinds(), ["swap", "unwrap", "unwrap"])
        self.assertEqual(len(chain.trades("SELL")), 1)


class RetryBackoffAndQuarantine(unittest.TestCase):
    def test_transient_quote_failures_use_bounded_backoff(self):
        chain = FakeChain(self)
        chain.quote_error = "HTTPSConnectionPool read timed out"
        delays = []
        for _ in range(5):
            result = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
            self.assertEqual(result["status"], "QUOTE_FAILED")
            record = next(iter(chain.state["sell_retry"].values()))
            delays.append((datetime.fromisoformat(record["next_retry_at"]) - chain.clock).total_seconds())
            blocked = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
            self.assertEqual(blocked["status"], "SELL_BACKOFF")
            chain.advance(delays[-1] + 1)
        self.assertEqual(delays, [60, 300, 900, 3600, 3600])
        self.assertEqual(chain.sent, [])

    def test_reproducible_stf_is_quarantined_and_not_retried_every_cycle(self):
        chain = FakeChain(self)
        chain.quote_error = "No live PinetSwap quote: 3000:{'message': 'execution reverted: STF'}"
        self.assertEqual(executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())["status"], "QUOTE_FAILED")
        chain.advance(61)
        quarantined = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(quarantined["status"], "QUARANTINED")
        stored = [i for i in chain.state["sell_intents"].values() if i["stage"] == "QUARANTINED"]
        self.assertEqual(len(stored), 1)
        for _ in range(5):
            chain.advance(3600)
            blocked = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
            self.assertEqual(blocked["status"], "SELL_QUARANTINED")
        self.assertEqual(len(chain.state["sell_intents"]), 2)
        self.assertEqual(chain.sent, [])

    def test_quarantine_is_scoped_to_affected_sell_reason(self):
        chain = FakeChain(self)
        chain.quote_error = "execution reverted: SPL"
        for _ in range(2):
            executor.evaluate_position(TOKEN, "TEST", "0.25", chain.clock.isoformat())
            chain.advance(61)
        self.assertEqual(executor.evaluate_position(TOKEN, "TEST", "0.25", chain.clock.isoformat())["status"],
                         "SELL_QUARANTINED")
        chain.quote_error = None
        chain.auto_mine = {"swap": lambda tx: chain.mine_swap(tx, 100, 10 * 10**18),
                           "unwrap": lambda tx: chain.mine_plain(tx)}
        stop = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(stop["status"], "CONFIRMED")

    def test_allowance_still_insufficient_after_approval_is_quarantined(self):
        chain = FakeChain(self)
        chain.allowance_value = 0
        chain.auto_mine = {"approval": lambda tx: chain.mine_plain(tx)}
        first = executor.evaluate_position(TOKEN, "TEST", "0.1", chain.clock.isoformat())
        self.assertEqual(first["status"], "ALLOWANCE_NOT_CONFIRMED")
        second = executor.startup_recovery()[0]
        self.assertEqual(second["status"], "QUARANTINED")
        self.assertEqual(chain.sent_kinds(), ["approval"])
        self.assertEqual(chain.state["pending_sells"], {})


class SellHealthVisibility(unittest.TestCase):
    def test_sell_exception_is_sell_error_and_buy_still_runs(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        harness.paper.write_text(json.dumps({"positions": {NEW_TOKEN: {"symbol": "NEW", "investment_sda": 20}}}),
                                 encoding="utf-8")
        with patch.object(executor, "evaluate_position", side_effect=RuntimeError("forced SELL failure")):
            result = harness.run()
        self.assertEqual(result["sell_health"], "SELL_ERROR")
        self.assertNotEqual(result["status"], "reconciled")
        self.assertIn("forced SELL failure", json.dumps(result["sell_health_detail"]))
        harness.buy.assert_called_once_with(NEW_TOKEN, "NEW", 20.0)
        saved = json.loads((Path(chain.tmp.name) / "bridge_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["sell_health"]["health"], "SELL_ERROR")

    def test_healthy_hold_is_reported(self):
        chain = FakeChain(self)
        harness = BridgeHarness(self, chain)
        result = harness.run()
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["sell_health"], "SELL_HOLD")

    def test_quarantine_is_reported(self):
        self.assertEqual(bridge.sell_health([{"action": "SELL_DECISION", "status": "SELL_QUARANTINED"}])["health"],
                         "SELL_QUARANTINED")
        self.assertEqual(bridge.sell_health([{"action": "SELL_RECOVERY", "status": "CONFIRMED"}])["health"],
                         "SELL_EXECUTED")
        self.assertEqual(bridge.sell_health([{"action": "SELL_RECOVERY", "status": "PENDING_CONFIRMATION"}])["health"],
                         "SELL_PENDING")


class PhantomManualSellRepair(unittest.TestCase):
    def _bugged_state(self, chain):
        """Exact production shape after the double count (scaled to 100 tokens / 20 SDA)."""
        chain.state["positions"][TOKEN].update({
            "initial_quantity": 100, "remaining_quantity": 60, "token_amount_raw": 60,
            "confirmed_sold_quantity": 20, "initial_cost_basis": "20.0",
            "remaining_cost_basis": "12.0", "sold_cost_basis": "4.0", "amount_sda": 12.0,
            "realized_proceeds": "5.0", "realized_pnl": "1.0",
            "profit_partial_done": True, "trailing_armed": True,
            "manual_reconciled": True, "manual_reconciled_quantity": 20,
            "highest_price_since_trailing_activation": "0.25",
        })
        chain.state["trades"] = [
            {"action": "MANUAL_SELL", "source": "wallet_reconciliation", "symbol": "TEST", "token": TOKEN,
             "sold_quantity_raw": 20, "cost_basis_sda": 4.0, "proceeds_sda": None,
             "realized_pnl_sda": None, "accounting_status": "PROCEEDS_UNKNOWN",
             "at": "2026-11-10T11:59:40+00:00"},
            {"action": "SELL", "symbol": "TEST", "token": TOKEN, "tx": "0xswap-prod",
             "sell_intent_id": "intent-prod", "sell_reason": "PROFIT_PARTIAL_25",
             "sold_quantity_raw": 20, "amount_sda": 4.0, "proceeds_sda": 5.0,
             "realized_pnl_sda": 1.0, "at": "2026-11-10T11:59:42+00:00"},
        ]
        chain.state["sell_intents"] = {"intent-prod": {
            "sell_intent_id": "intent-prod", "token": TOKEN, "position_id": "0xbuy",
            "sell_reason": "PROFIT_PARTIAL_25", "stage": "RECONCILED", "status": "RECONCILED",
            "swap_tx": "0xswap-prod", "confirmed_sold_quantity_raw": 20,
            "confirmed_proceeds_raw": 5 * 10**18, "created_at": "2026-11-10T11:58:30+00:00",
            "confirmed_at": "2026-11-10T11:59:42+00:00",
        }}
        chain.balances[TOKEN] = 80
        chain.mine_swap("0xswap-prod", 20, 5 * 10**18)
        chain.balances[TOKEN] = 80

    def test_phantom_manual_sell_is_superseded_and_position_restored(self):
        chain = FakeChain(self)
        self._bugged_state(chain)
        repairs = executor.repair_superseded_manual_sells()
        self.assertEqual(len(repairs), 1)
        pos = chain.position()
        self.assertEqual(pos["remaining_quantity"], 80)
        self.assertEqual(Decimal(pos["remaining_cost_basis"]), Decimal("16.0"))
        self.assertNotIn("manual_reconciled", pos)
        manual = chain.trades("MANUAL_SELL")[0]
        self.assertEqual(manual["superseded_by"], "intent-prod")
        self.assertEqual(manual["accounting_status"], "SUPERSEDED_BY_AUTOMATIC_SELL")
        self.assertEqual(len(chain.state["trades"]), 2)  # evidence preserved
        self.assertEqual(repairs[0]["before"]["remaining_quantity"], 60)
        self.assertEqual(executor.repair_superseded_manual_sells(), [])
        self.assertEqual(executor.reconcile_position(TOKEN)["wallet"]["status"], "UNCHANGED")

    def test_genuine_manual_sale_is_not_superseded(self):
        chain = FakeChain(self)
        self._bugged_state(chain)
        chain.balances[TOKEN] = 60  # wallet really holds only 60: the manual sale was real
        self.assertEqual(executor.repair_superseded_manual_sells(), [])
        self.assertNotIn("superseded_by", chain.trades("MANUAL_SELL")[0])
        self.assertEqual(chain.position()["remaining_quantity"], 60)

    def test_repair_requires_matching_chain_receipt(self):
        chain = FakeChain(self)
        self._bugged_state(chain)
        chain.receipts.clear()
        self.assertEqual(executor.repair_superseded_manual_sells(), [])
        self.assertEqual(chain.position()["remaining_quantity"], 60)


class SwingV2RulesUnchanged(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(policy.decide({}, "-0.499", "1")["action"], "HOLD")
        self.assertEqual(policy.decide({}, "-0.50", "1"), {"action": "FULL_SELL", "reason": "LOSS_LIMIT"})
        self.assertEqual(policy.decide({}, "0.249", "1")["action"], "HOLD")
        self.assertEqual(policy.decide({}, "0.25", "1")["action"], "PARTIAL_SELL")
        self.assertEqual(policy.decide({}, "0.25", "1")["fraction"], "0.20")
        self.assertEqual(policy.decide({}, "0.50", "1"), {"action": "FULL_SELL", "reason": "HARD_TP"})
        armed = {"profit_partial_done": True, "trailing_armed": True,
                 "highest_price_since_trailing_activation": "1.0"}
        self.assertEqual(policy.decide(armed, "0.30", "0.85")["reason"], "TRAILING_STOP")
        self.assertEqual(policy.decide(armed, "0.30", "0.851")["action"], "HOLD")
        self.assertEqual(policy.decide(armed, "0.30", "0.851")["trailing_price"], "0.850")
        self.assertEqual(policy.PARTIAL_FRACTION, Decimal("0.20"))
        self.assertEqual(policy.TRAILING_DISTANCE, Decimal("0.15"))
        self.assertEqual(policy.LOSS_EXIT_PNL, Decimal("-0.50"))
        self.assertEqual(policy.PARTIAL_TRIGGER_PNL, Decimal("0.25"))
        self.assertEqual(policy.HARD_TP_PNL, Decimal("0.50"))
        self.assertFalse(hasattr(policy, "TIME_EXIT_SECONDS"))


if __name__ == "__main__":
    unittest.main()
