"""Shared BOT-position dust policy: negligible wallet residuals close the BOT
position without any SELL, MANUAL_SELL or accounting disposal.

All chain access is the in-memory FakeChain; no RPC, signing or broadcast happens.
"""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import real_bot_menu as rb
import real_bot_views as views
import real_swing_sell_executor as executor
import real_trader
import real_trading_config as cfg
from tests.test_real_sell_p0_fixes import TOKEN, BridgeHarness, FakeChain

XEN_INITIAL = 317664255649068659320
XEN_SOLD = 317664200000000000000
XEN_RESIDUAL = 55649068659320
ONE_TOKEN = 10**18


def _chain(test, quantity=XEN_INITIAL, cost="20", decimals=18):
    chain = FakeChain(test, quantity=quantity, cost=cost)
    patcher = patch.object(real_trader, "token_decimals", return_value=decimals)
    patcher.start()
    test.addCleanup(patcher.stop)
    return chain


def _xen_dust_state(chain):
    """Production XEN shape after the manual SELL was reconciled as PARTIAL."""
    chain.state["positions"][TOKEN].update({
        "remaining_quantity": XEN_RESIDUAL, "token_amount_raw": XEN_RESIDUAL,
        "wallet_available_quantity": XEN_RESIDUAL,
        "remaining_cost_basis": "0.000003503640568285835531213455235",
        "amount_sda": 3.5036405682858355e-06, "initial_quantity": XEN_INITIAL,
        "initial_cost_basis": "19.999999999999996", "manual_reconciled": True,
        "manual_reconciled_quantity": XEN_SOLD, "last_market_price": "0.06848093633288181",
    })
    chain.state["trades"].append({"action": "MANUAL_SELL", "source": "wallet_reconciliation",
                                  "token": TOKEN, "sold_quantity_raw": XEN_SOLD,
                                  "cost_basis_sda": 19.99999649635943, "proceeds_sda": None,
                                  "realized_pnl_sda": None, "accounting_status": "PROCEEDS_UNKNOWN",
                                  "at": "2026-11-11T16:48:31.845020+00:00"})
    chain.balances[TOKEN] = XEN_RESIDUAL


class SharedDustPolicy(unittest.TestCase):
    def test_threshold_respects_token_decimals(self):
        self.assertEqual(cfg.DUST_TOKEN_QUANTITY, Decimal("0.001"))
        self.assertEqual(cfg.dust_threshold_raw(18), 10**15)
        self.assertEqual(cfg.dust_threshold_raw(6), 1000)
        self.assertEqual(cfg.dust_threshold_raw(0), 0)

    def test_classification_boundaries(self):
        self.assertFalse(cfg.is_dust_quantity(0, 18))
        self.assertTrue(cfg.is_dust_quantity(XEN_RESIDUAL, 18))
        self.assertTrue(cfg.is_dust_quantity(10**15, 18))
        self.assertFalse(cfg.is_dust_quantity(10**15 + 1, 18))
        self.assertFalse(cfg.is_dust_quantity(2 * 10**15, 18))
        self.assertTrue(cfg.is_dust_quantity(999, 6))
        self.assertFalse(cfg.is_dust_quantity(1001, 6))
        self.assertFalse(cfg.is_dust_quantity(1, 0))

    def test_value_guard_keeps_valuable_residuals(self):
        self.assertFalse(cfg.is_dust_quantity(10**15, 18, cost_basis_sda="5"))
        self.assertFalse(cfg.is_dust_quantity(10**15, 18, price_sda="1000"))
        self.assertTrue(cfg.is_dust_quantity(10**15, 18, cost_basis_sda="0.001", price_sda="0.5"))

    def test_accounting_rebuild_uses_the_shared_threshold(self):
        source = open("scripts/rebuild_real_accounting.py", encoding="utf-8").read()
        self.assertIn("from real_trading_config import DUST_TOKEN_QUANTITY", source)
        self.assertIn("qty<=DUST_TOKEN_QUANTITY", source)
        self.assertNotIn('quantity<=Decimal("0.001")', source)
        self.assertNotIn('qty<=Decimal("0.001")', source)


class WalletDustReconciliation(unittest.TestCase):
    def test_wallet_zero_still_closes_as_manual(self):
        chain = _chain(self)
        chain.balances[TOKEN] = 0
        result = executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(result["status"], "CLOSED_MANUAL_RECONCILED")
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(chain.state.get("dust_reconciliations", []), [])

    def test_manual_full_sell_leaving_xen_like_dust_closes_bot_position(self):
        chain = _chain(self)
        chain.balances[TOKEN] = XEN_RESIDUAL
        result = executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(result["status"], "DUST_RECONCILED")
        self.assertTrue(result["manual_disposal"])
        self.assertNotIn(TOKEN, chain.state["positions"])
        manual = chain.trades("MANUAL_SELL")
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["sold_quantity_raw"], XEN_INITIAL - XEN_RESIDUAL)
        self.assertEqual(chain.trades("SELL"), [])
        self.assertEqual(len(chain.state["manual_disposals"]), 1)
        record, = chain.state["dust_reconciliations"]
        self.assertEqual(record["action"], "DUST_RECONCILIATION")
        self.assertEqual(record["reason"], "NEGLIGIBLE_RESIDUAL_AFTER_DISPOSAL")
        self.assertEqual(record["wallet_quantity_raw"], XEN_RESIDUAL)
        self.assertEqual(Decimal(record["wallet_quantity"]), Decimal("0.00005564906865932"))
        self.assertLess(Decimal(record["remaining_cost_basis"]), Decimal("0.00001"))
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertEqual(chain.state["sell_intents"], {})
        self.assertEqual(chain.balances[TOKEN], XEN_RESIDUAL)
        self.assertEqual(chain.sent, [])

    def test_already_reconciled_xen_dust_closes_without_new_manual_sell(self):
        chain = _chain(self)
        _xen_dust_state(chain)
        trades_before = json.dumps(chain.state["trades"])
        result = executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(result["status"], "DUST_RECONCILED")
        self.assertFalse(result["manual_disposal"])
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertEqual(json.dumps(chain.state["trades"]), trades_before)
        self.assertEqual(chain.state.get("manual_disposals", []), [])
        self.assertEqual(chain.state["daily_realized_pnl_sda"], 0.0)
        self.assertEqual(len(chain.state["dust_reconciliations"]), 1)
        self.assertEqual(chain.sent, [])

    def test_exactly_at_threshold_is_dust(self):
        chain = _chain(self)
        chain.balances[TOKEN] = 10**15
        self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "DUST_RECONCILED")
        self.assertNotIn(TOKEN, chain.state["positions"])

    def test_above_threshold_remains_active(self):
        chain = _chain(self)
        chain.balances[TOKEN] = 2 * 10**15  # 0.002 token
        result = executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(result["status"], "MANUAL_RECONCILED")
        self.assertEqual(chain.position()["remaining_quantity"], 2 * 10**15)
        self.assertEqual(chain.state.get("dust_reconciliations", []), [])

    def test_six_decimal_token(self):
        chain = _chain(self, quantity=100 * 10**6, decimals=6)
        chain.balances[TOKEN] = 1001
        self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "MANUAL_RECONCILED")
        self.assertEqual(chain.position()["remaining_quantity"], 1001)
        chain.balances[TOKEN] = 999
        self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "DUST_RECONCILED")
        self.assertNotIn(TOKEN, chain.state["positions"])

    def test_valuable_residual_is_not_dust(self):
        chain = _chain(self, quantity=2 * 10**15, cost="20")  # 0.002 token worth 20 SDA
        chain.balances[TOKEN] = 10**15
        self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "MANUAL_RECONCILED")
        self.assertIn(TOKEN, chain.state["positions"])

    def test_unknown_decimals_never_classifies_dust(self):
        chain = _chain(self)
        chain.balances[TOKEN] = XEN_RESIDUAL
        with patch.object(real_trader, "token_decimals", side_effect=RuntimeError("Unknown decimals")):
            self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "MANUAL_RECONCILED")
        self.assertIn(TOKEN, chain.state["positions"])

    def test_restart_is_idempotent(self):
        chain = _chain(self)
        _xen_dust_state(chain)
        stale = json.loads(json.dumps(chain.state["positions"][TOKEN]))
        executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(executor.reconcile_position_wallet(TOKEN)["status"], "NO_POSITION")
        chain.state["positions"][TOKEN] = stale  # restart from an older checkpoint
        executor.reconcile_position_wallet(TOKEN)
        self.assertEqual(len(chain.state["dust_reconciliations"]), 1)
        self.assertEqual(chain.trades("MANUAL_SELL")[-1]["sold_quantity_raw"], XEN_SOLD)
        self.assertEqual(len(chain.trades("MANUAL_SELL")), 1)
        self.assertEqual(chain.sent, [])


class DustNeverSold(unittest.TestCase):
    def test_sell_v2_ignores_dust_even_at_extreme_prices(self):
        chain = _chain(self)
        _xen_dust_state(chain)
        for price in ("0.0001", "1000"):
            result = executor.evaluate_position(TOKEN, "XEN", price, chain.clock.isoformat(),
                                                wallet_reconciled=True)
            self.assertEqual(result, {"action": "HOLD", "status": "DUST_ONLY", "token": TOKEN})
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertEqual(chain.sent, [])

    def test_bridge_cycles_close_dust_once_without_sell(self):
        chain = _chain(self)
        _xen_dust_state(chain)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.0001")  # would be far below the -50% loss limit
        first = harness.run()
        dust = [a for a in first["actions"] if a.get("action") == "DUST_RECONCILIATION"]
        self.assertEqual(len(dust), 1)
        self.assertEqual(dust[0]["status"], "DUST_CLOSED")
        self.assertFalse([a for a in first["actions"] if a.get("action") == "SELL_DECISION"])
        self.assertEqual(first["sell_health"], "SELL_HOLD")
        for _ in range(3):
            chain.advance(60)
            again = harness.run()
            self.assertFalse([a for a in again["actions"]
                              if a.get("action") in {"DUST_RECONCILIATION", "MANUAL_SELL_RECONCILIATION"}])
        self.assertEqual(len(chain.state["dust_reconciliations"]), 1)
        self.assertEqual(len(chain.trades("MANUAL_SELL")), 1)
        self.assertEqual(chain.trades("SELL"), [])
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertEqual(chain.sent, [])

    def test_automatic_full_sell_leaving_dust_closes_without_manual_sell(self):
        chain = _chain(self)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.03")  # ~ -52% versus 20 SDA / 317.66 tokens
        harness.run()
        self.assertEqual(chain.sent_kinds(), ["swap"])
        chain.mine_swap(chain.sent[0][1], XEN_INITIAL - 5000, 9 * 10**18)
        chain.advance(60)
        second = harness.run()
        self.assertNotIn(TOKEN, chain.state["positions"])
        self.assertIn("DUST_RECONCILIATION", json.dumps(second["actions"]))
        chain.mine_plain(chain.sent[1][1])
        chain.advance(60)
        harness.run()
        self.assertEqual(chain.trades("MANUAL_SELL"), [])
        self.assertEqual(len(chain.trades("SELL")), 1)
        self.assertEqual(chain.state.get("manual_disposals", []), [])
        self.assertEqual(len(chain.state["dust_reconciliations"]), 1)
        self.assertEqual(chain.state["pending_sells"], {})
        self.assertEqual(chain.sent_kinds(), ["swap", "unwrap"])
        self.assertEqual(chain.balances[TOKEN], 5000)

    def test_automatic_partial_sell_keeps_material_position(self):
        chain = _chain(self)
        harness = BridgeHarness(self, chain)
        harness.price = Decimal("0.08")  # > +25% versus entry 0.06296
        harness.run()
        chain.mine_swap(chain.sent[0][1], XEN_INITIAL // 5, 5 * 10**18)
        chain.advance(60)
        harness.run()
        chain.mine_plain(chain.sent[1][1])
        chain.advance(60)
        harness.run()
        position = chain.position()
        self.assertIsNotNone(position)
        self.assertEqual(position["remaining_quantity"], XEN_INITIAL - XEN_INITIAL // 5)
        self.assertEqual(chain.state.get("dust_reconciliations", []), [])
        self.assertEqual(chain.trades("MANUAL_SELL"), [])

    def test_manual_partial_keeps_material_position(self):
        chain = _chain(self)
        chain.balances[TOKEN] = XEN_INITIAL // 2
        self.assertEqual(executor.reconcile_position(TOKEN)["wallet"]["status"], "MANUAL_RECONCILED")
        self.assertEqual(chain.position()["remaining_quantity"], XEN_INITIAL // 2)
        self.assertAlmostEqual(float(chain.position()["remaining_cost_basis"]), 10.0)


class DustCapacity(unittest.TestCase):
    def _fill(self, chain):
        for i in range(9):
            token = "0x" + f"{i + 1:040x}"
            chain.state["positions"][token] = {"token": token, "token_amount_raw": 10**20,
                                               "amount_sda": 20.0}

    def test_dust_frees_position_slot_and_capital(self):
        chain = _chain(self)
        self._fill(chain)
        _xen_dust_state(chain)
        chain.state["positions"][TOKEN]["amount_sda"] = 20.0  # worst case stale cost field
        with patch.object(cfg, "TRADING_WALLET_CAPITAL_SDA", 200.0):
            with self.assertRaises(RuntimeError):
                real_trader.safety_check(20, real_trader.load_state())
            executor.reconcile_position(TOKEN)
            state = real_trader.load_state()
            self.assertEqual(len(state["positions"]), 9)
            real_trader.safety_check(20, state)  # one slot and 20 SDA available again
        self.assertEqual(chain.sent, [])


def _telegram_dashboard(runtime, metadata, accounting):
    return SimpleNamespace(load=lambda path, default:
        accounting if path == "real_accounting_state.json" else
        runtime if path == "real_trade_state.json" else
        metadata if path == "token_metadata.json" else default)


def test_telegram_portfolio_excludes_dust_only_bot_position(monkeypatch):
    xen = "0x53167ff5810ff1886fc554e672994c232126383e"
    jsft = "0xb4ac5dd62f700e271bffb52aeae1dba40135427f"
    accounting = {"schema_version": 1, "bot_reconciliation_status": "COMPLETE",
                  "bot_unresolved_transactions": [], "realized_pnl_sda": "2.0678",
                  "open_positions": [], "buy_transactions": [], "sell_transactions": [], "statistics": {}}
    runtime = {"positions": {
        xen: {"token": xen, "symbol": "0x53167f", "remaining_quantity": XEN_RESIDUAL,
              "remaining_cost_basis": "0.0000035036", "last_market_price": "0.0684"},
        jsft: {"token": jsft, "symbol": "JSFT", "remaining_quantity": str(60 * ONE_TOKEN),
               "remaining_cost_basis": "20"},
    }}
    metadata = {xen: {"symbol": "XEN", "decimals": 18}, jsft: {"symbol": "JSFT", "decimals": "18"}}
    dashboard = _telegram_dashboard(runtime, metadata, accounting)
    monkeypatch.setattr(rb, "_load_worker_status", lambda _d: {"enabled": True, "dry_run": False})
    monkeypatch.setattr(rb, "_status", lambda _w: "🟢 LIVE")
    wallet_snapshot = {"native_sda": 100.0, "wallet_address": rb.BOT_WALLET_ADDRESS,
                       "token_balances_loaded": True, "holdings": [
                           {"address": xen, "symbol": "XEN", "amount": Decimal(XEN_RESIDUAL) / ONE_TOKEN,
                            "market_value_sda": Decimal("0.0000038")},
                           {"address": jsft, "symbol": "JSFT", "amount": 60, "market_value_sda": Decimal("24")}]}
    monkeypatch.setattr(views, "_ORIGINAL_FETCH_WALLET_SNAPSHOT", lambda include_prices=False: wallet_snapshot)
    monkeypatch.setattr(views, "_price_holding", lambda holding, wallet, analysis=None: holding)
    snapshot = views._tracked_wallet_snapshot(accounting, dashboard)
    assert {item["address"] for item in snapshot["holdings"]} == {jsft}
    monkeypatch.setattr(views, "_tracked_wallet_snapshot", lambda _a, _d=None: snapshot)
    data = views._build_dashboard_data(dashboard)
    assert [p["token"] for p in data["positions"]] == [jsft]
    text = views._portfolio_text(data)
    assert "XEN" not in text
    assert "JSFT" in text
    assert data["open_cost"] == pytest.approx(20)
