"""ABI encoding regression test for the real swap executor."""
import unittest

import real_trader


TOKEN_IN = "0x" + "11" * 20
TOKEN_OUT = "0x" + "22" * 20
RECIPIENT = "0x" + "33" * 20


class SwapCalldataTests(unittest.TestCase):
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
