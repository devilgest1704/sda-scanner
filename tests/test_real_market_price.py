import unittest
from datetime import datetime, timezone
from decimal import Decimal

import real_market_price as market
import real_swing_sell_report as report

NOW = datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)
TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


class SharedMarketPriceTests(unittest.TestCase):
    def test_price_is_mapped_by_exact_normalized_contract(self):
        snapshot = {"updated_at": "2026-11-10T08:29:00Z", "tokens": {
            TOKEN: {"price_in_sda": "0.25", "last_transaction": "2026-11-10T08:29:00Z"},
            OTHER: {"price_in_sda": "9.99", "last_transaction": "2026-11-10T08:29:00Z"},
        }}
        result = market.scanner_market_price(TOKEN.upper().replace("0X", "0x"), snapshot, NOW)
        self.assertEqual(result["price_sda"], 0.25)
        self.assertIsNone(market.scanner_market_price("0x" + "3" * 40, snapshot, NOW))

    def test_fresh_market_price_is_accepted(self):
        snapshot = {"updated_at": "2026-11-10T08:29:00Z", "tokens": {
            TOKEN: {"price_in_sda": "0.25", "last_transaction": "2026-11-10T08:29:30Z"}}}
        result = market.resolve_market_price(TOKEN, snapshot, NOW)
        self.assertEqual(result["source"], "scanner_market_analysis")
        self.assertAlmostEqual(result["price_sda"], 0.25)

    def test_stale_scanner_and_supabase_prices_are_rejected(self):
        snapshot = {"updated_at": "2026-11-10T08:00:00Z", "tokens": {
            TOKEN: {"price_in_sda": "0.25", "last_transaction": "2026-11-10T08:29:30Z"}}}
        lookup = lambda _token: {"price_sda": "0.24", "timestamp": "2026-11-10T08:00:00Z"}
        self.assertIsNone(market.resolve_market_price(TOKEN, snapshot, NOW, supabase_lookup=lookup))

    def test_fresh_supabase_trade_is_accepted_when_scanner_is_stale(self):
        snapshot = {"updated_at": "2026-11-10T08:00:00Z", "tokens": {}}
        lookup = lambda _token: {"price_sda": "0.24", "timestamp": "2026-11-10T08:29:30Z"}
        result = market.resolve_market_price(TOKEN, snapshot, NOW, supabase_lookup=lookup)
        self.assertEqual(result["source"], "supabase_transaction")
        self.assertAlmostEqual(result["price_sda"], 0.24)

    def test_live_quote_timestamp_acquired_after_resolver_start_is_accepted(self):
        snapshot = {"updated_at": "2026-11-10T08:00:00Z", "tokens": {}}
        quote = lambda _token: {"price_sda": "0.26", "timestamp": "2026-11-10T08:30:02Z"}
        result = market.resolve_market_price(TOKEN, snapshot, NOW, reference_quote_lookup=quote)
        self.assertIsNotNone(result)
        self.assertEqual(result["age_seconds"], 0)

    def test_far_future_quote_timestamp_is_rejected(self):
        quote = lambda _token: {"price_sda": "0.26", "timestamp": "2026-11-10T08:31:00Z"}
        result = market.resolve_market_price(TOKEN, {}, NOW, reference_quote_lookup=quote)
        self.assertIsNone(result)

    def test_live_buy_reference_is_used_after_observed_prices_expire(self):
        snapshot = {"updated_at": "2026-11-10T08:00:00Z", "tokens": {}}
        quote = lambda _token: {"price_sda": "0.26", "timestamp": "2026-11-10T08:30:00Z",
                                "source": "PinetSwap 1 SDA BUY-side reference quote"}
        result = market.resolve_market_price(TOKEN, snapshot, NOW, reference_quote_lookup=quote)
        self.assertEqual(result["source"], "PinetSwap 1 SDA BUY-side reference quote")
        self.assertEqual(result["quote_side"], "BUY_REFERENCE")
        self.assertEqual(result["quote_amount_sda"], 1)

    def test_executable_sell_quote_is_not_a_market_price_input(self):
        quote = {"price_sda": "0.30", "timestamp": "2026-11-10T08:30:00Z",
                 "source": "PinetSwap 1 SDA BUY-side reference quote"}
        result = market.resolve_market_price(TOKEN, {}, NOW,
            reference_quote_lookup=lambda _token: quote)
        self.assertNotIn("sell_quote", result)
        self.assertEqual(result["quote_side"], "BUY_REFERENCE")

    def test_decimals_current_value_and_pnl_are_exact(self):
        state = {"positions": {TOKEN: {
            "remaining_quantity": 12_345_678, "remaining_cost_basis": "20",
            "wallet_available_quantity": 12_345_678}}}
        analysis = {"updated_at": "2026-11-10T08:29:00Z", "tokens": {
            TOKEN: {"price_in_sda": "2", "last_transaction": "2026-11-10T08:29:00Z"}}}
        metadata = {TOKEN: {"symbol": "DEC6", "decimals": "6"}}
        row = report.build_decision_rows(state, analysis, metadata, now=NOW)[0]
        self.assertAlmostEqual(row["quantity"], 12.345678)
        self.assertAlmostEqual(row["market_value_sda"], 24.691356)
        self.assertAlmostEqual(row["market_pnl_sda"], 4.691356)
        self.assertAlmostEqual(row["pnl_pct"], 23.45678)

    def test_xen_sign_reversal_is_detected_from_current_reference(self):
        xen = "0x53167ff5810ff1886fc554e672994c232126383e"
        state = {"positions": {xen: {
            "remaining_quantity": 317664255649068700000,
            "remaining_cost_basis": "20.080973000001133622",
            "wallet_available_quantity": 317664255649068700000}}}
        analysis = {"updated_at": "2026-11-10T08:29:00Z", "tokens": {
            xen: {"price_in_sda": "0.065927", "last_transaction": "2026-11-10T08:29:00Z"}}}
        metadata = {xen: {"symbol": "XEN", "decimals": "18"}}
        row = report.build_decision_rows(state, analysis, metadata, now=NOW)[0]
        self.assertGreater(row["market_pnl_sda"], 0)
        self.assertGreater(row["pnl_pct"], 0)
        self.assertLess(18.85563804280111 - 20.080973000001133622, 0)

    def test_portfolio_and_sell_v2_resolve_the_same_market_price(self):
        analysis = {"updated_at": "2026-11-10T08:29:00Z", "tokens": {
            TOKEN: {"price_in_sda": "0.205", "last_transaction": "2026-11-10T08:29:00Z"}}}
        portfolio_mark = market.resolve_market_price(TOKEN, analysis, NOW)
        state = {"positions": {TOKEN: {"remaining_quantity": 100 * 10**18,
            "remaining_cost_basis": "20", "wallet_available_quantity": 100 * 10**18}}}
        metadata = {TOKEN: {"symbol": "TEST", "decimals": "18"}}
        row = report.build_decision_rows(state, analysis, metadata, now=NOW)[0]
        self.assertEqual(row["decision_price"], portfolio_mark["price_sda"])
        self.assertEqual(row["price_source"], portfolio_mark["source"])


if __name__ == "__main__":
    unittest.main()
