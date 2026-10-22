"""Regression tests for market timestamp windows and future-price exclusion.

Run: python -m unittest discover -s tests -p 'test_market_time_windows.py'
Avoid importing market_scanner directly: it executes its live fetch on import.
"""
import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "market_scanner.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NAMES = {
    "parse_timestamp", "get_transactions_in_window", "calculate_window",
    "find_price_before", "calculate_price_change", "analyze_token",
}
functions = [node for node in TREE.body
             if isinstance(node, ast.FunctionDef) and node.name in NAMES]
namespace = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone,
             "MIN_RECENT_VOLUME_SDA": 250}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"),
     namespace)


def tx(at, price=1.0, volume=10.0, side="buy"):
    return {"timestamp": at.isoformat(), "price": price,
            "volume": volume, "type": side}


class MarketWindowRegression(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        namespace["now_utc"] = lambda: self.now

    def test_previous_bucket_excludes_recent_and_future(self):
        history = [tx(self.now - timedelta(minutes=25), volume=20),
                   tx(self.now - timedelta(minutes=5), volume=100),
                   tx(self.now + timedelta(minutes=5), volume=1000)]
        previous = namespace["get_transactions_in_window"](
            history, self.now - timedelta(minutes=15), 15)
        current = namespace["get_transactions_in_window"](
            history, self.now, 15)
        self.assertEqual(sum(x["volume"] for x in previous), 20)
        self.assertEqual(sum(x["volume"] for x in current), 100)

    def test_future_trade_does_not_set_price_or_volume(self):
        history = [tx(self.now - timedelta(minutes=70), price=1),
                   tx(self.now - timedelta(minutes=5), price=2, volume=30),
                   tx(self.now + timedelta(minutes=5), price=99, volume=5000)]
        result = namespace["analyze_token"]({"transactions": history})
        self.assertEqual(result["price_in_sda"], 2)
        self.assertEqual(result["flow"]["15m"]["total_volume"], 30)
        self.assertFalse(result["active"])

    def test_only_future_transactions_have_no_analysis(self):
        result = namespace["analyze_token"]({
            "transactions": [tx(self.now + timedelta(minutes=1))]})
        self.assertIsNone(result)

    def test_volume_acceleration_uses_nonoverlapping_buckets(self):
        history = [tx(self.now - timedelta(minutes=25), volume=20),
                   tx(self.now - timedelta(minutes=5), volume=40),
                   tx(self.now + timedelta(minutes=5), volume=1000)]
        result = namespace["analyze_token"]({"transactions": history})
        self.assertAlmostEqual(result["volume_acceleration_15m_pct"], 100)


if __name__ == "__main__":
    unittest.main()
