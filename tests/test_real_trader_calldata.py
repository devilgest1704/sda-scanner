"""ABI encoding regression test for the real swap executor."""
import unittest
from datetime import datetime, timezone

import real_trader


TOKEN_IN = "0x" + "11" * 20
TOKEN_OUT = "0x" + "22" * 20
RECIPIENT = "0x" + "33" * 20


class SwapCalldataTests(unittest.TestCase):
    def test_daily_pnl_resets_when_utc_date_changes(self):
        state = {"daily_pnl_date": "2000-01-01", "daily_realized_pnl_sda": -12.5}
        real_trader._reset_daily_pnl_if_needed(state)
        self.assertEqual(state["daily_pnl_date"], datetime.now(timezone.utc).date().isoformat())
        self.assertEqual(state["daily_realized_pnl_sda"], 0.0)

    def test_daily_pnl_is_preserved_on_same_utc_date(self):
        today = datetime.now(timezone.utc).date().isoformat()
        state = {"daily_pnl_date": today, "daily_realized_pnl_sda": -12.5}
        real_trader._reset_daily_pnl_if_needed(state)
        self.assertEqual(state["daily_realized_pnl_sda"], -12.5)

    def test_daily_loss_guard_blocks_new_buy(self):
        state = {
            "positions": {},
            "daily_pnl_date": datetime.now(timezone.utc).date().isoformat(),
            "daily_realized_pnl_sda": -real_trader.cfg.MAX_DAILY_LOSS_SDA,
        }
        with self.assertRaisesRegex(RuntimeError, "Daily real-trading loss limit"):
            real_trader.safety_check(real_trader.cfg.POSITION_DEFAULT_SDA, state)

    def test_real_exposure_uses_recorded_amount_sda(self):
        state = {
            "positions": {"0x" + "44" * 20: {"amount_sda": 190.0}},
            "pending_buys": {},
            "daily_pnl_date": datetime.now(timezone.utc).date().isoformat(),
            "daily_realized_pnl_sda": 0.0,
        }
        with self.assertRaisesRegex(RuntimeError, "capital cap"):
            real_trader.safety_check(15.0, state)

    def test_pending_buys_reserve_both_slot_and_capital(self):
        state = {
            "positions": {},
            "pending_buys": {"0x" + "44" * 20: {"position_sda": 190.0}},
            "daily_pnl_date": datetime.now(timezone.utc).date().isoformat(),
            "daily_realized_pnl_sda": 0.0,
        }
        with self.assertRaisesRegex(RuntimeError, "capital cap"):
            real_trader.safety_check(15.0, state)

    def test_exact_input_single_encodes_both_tokens_and_all_eight_words(self):
        data = real_trader.build_exact_input_single(
            TOKEN_IN, TOKEN_OUT, 3000, RECIPIENT, 123456, 789, 654
        )
        self.assertEqual(data[:10], real_trader.cfg.BUY_SELECTOR)
        words = [data[10 + i * 64: 10 + (i + 1) * 64] for i in range(8)]
        self.assertEqual(words[0], real_trader.address_word(TOKEN_IN))
        self.assertEqual(words[1], real_trader.address_word(TOKEN_OUT))
        self.assertEqual(int(words[2], 16), 3000)
        self.assertEqual(words[3], real_trader.address_word(RECIPIENT))
        self.assertEqual(int(words[4], 16), 123456)
        self.assertEqual(int(words[5], 16), 789)
        self.assertEqual(int(words[6], 16), 654)
        self.assertEqual(int(words[7], 16), 0)
        self.assertEqual(len(data), 10 + 8 * 64)


if __name__ == "__main__":
    unittest.main()
