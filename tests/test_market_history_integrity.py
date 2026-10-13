import unittest
from datetime import datetime, timedelta, timezone

from tools.market_history_integrity import (
    append_transaction, compact_history, transaction_hash,
)


def row(index, hash_value=None, **extra):
    at = datetime(2026, 10, 13, tzinfo=timezone.utc) + timedelta(minutes=index)
    result = {
        "hash": hash_value if hash_value is not None else f"0x{index:06x}",
        "timestamp": at.isoformat(),
        "price": 1.0 + index * .001,
        "volume": 25.0,
        "type": "buy",
    }
    result.update(extra)
    return result


class MarketHistoryIntegrityTests(unittest.TestCase):
    def test_full_history_rejects_old_replay_beyond_200_items(self):
        history, hashes, removed = compact_history(
            [row(i) for i in range(350)])
        self.assertEqual(removed, 0)
        self.assertFalse(append_transaction(history, row(0), hashes))
        self.assertEqual(len(history), 350)
        self.assertTrue(append_transaction(history, row(351), hashes))
        self.assertFalse(append_transaction(history, row(351), hashes))
        self.assertEqual(len(history), 351)

    def test_repairs_previous_duplicate_history_without_deleting_unique_events(self):
        initial = [row(i) for i in range(315)]
        # Legacy newest-first fetches can repeat much older transactions.
        source = initial + [row(2), row(3), row(2)]
        repaired, hashes, removed = compact_history(source)
        self.assertEqual(removed, 3)
        self.assertEqual(len(repaired), 315)
        self.assertEqual(len(hashes), 315)
        self.assertEqual([item["hash"] for item in repaired],
                         [item["hash"] for item in initial])

    def test_reverse_sorted_supabase_batch_keeps_latest_retained_history(self):
        source = list(reversed([row(i) for i in range(12)]))
        repaired, hashes, removed = compact_history(source, limit=8)
        self.assertEqual(removed, 0)
        self.assertEqual(len(hashes), 8)
        self.assertEqual([x["hash"] for x in repaired],
                         [row(i)["hash"] for i in range(4, 12)])
        self.assertTrue(append_transaction(repaired, row(1), hashes))
        repaired_again, _, _ = compact_history(repaired, limit=8)
        self.assertEqual(repaired_again, repaired[:-1])

    def test_only_hash_identifies_duplicate_not_price_timestamp_or_volume(self):
        a = row(5, "0xFIRST")
        b = row(5, "0xSECOND")
        c = row(5, "")
        d = row(5, "")
        repaired, hashes, removed = compact_history([a, b, c, d])
        self.assertEqual(len(repaired), 4)
        self.assertEqual(removed, 0)
        self.assertEqual(transaction_hash(a), "0xfirst")
        self.assertFalse(append_transaction(repaired, row(5, "0xFirst"), hashes))
        self.assertTrue(append_transaction(repaired, row(5, ""), hashes))

    def test_oldest_timestamp_wins_for_same_hash(self):
        original = row(3, "0xdead")
        later = row(9, "0xdead", price=99)
        repaired, _, removed = compact_history([later, original])
        self.assertEqual(repaired, [original])
        self.assertEqual(removed, 1)

    def test_corrupt_history_fails_closed(self):
        for bad in (None, {}, [None], ["not a transaction"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    compact_history(bad)
        with self.assertRaises(ValueError):
            compact_history([], limit=0)


if __name__ == "__main__":
    unittest.main()
