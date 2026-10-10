import unittest

from tools.telegram_delivery import split_report, send_report, _units, MAX_UNITS


class TelegramDeliveryTests(unittest.TestCase):
    def test_split_long_unicode_report_without_losing_content(self):
        source = "TOP BUY\n" + ("🚀 Price +1.3% SDA\n" * 600)
        parts = split_report(source)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(_units(p) <= MAX_UNITS for p in parts))
        self.assertEqual("\n".join(parts), source.strip())

    def test_keyboard_only_on_last_part_and_api_verified(self):
        requests = []
        def api(method, payload):
            requests.append((method, payload))
            return {"ok": True}
        count = send_report(api, "🚀\n" * 1600, {"inline_keyboard": [[{"text": "Refresh", "callback_data": "REFRESH"}]]}, "123")
        self.assertGreater(count, 1)
        self.assertTrue(all(method == "sendMessage" for method, _ in requests))
        self.assertTrue(all("reply_markup" not in payload for _,payload in requests[:-1]))
        self.assertIn("reply_markup", requests[-1][1])

    def test_no_silent_success_if_telegram_rejects(self):
        def reject(method, payload):
            return {"ok": False, "description": "Bad Request"}
        with self.assertRaisesRegex(RuntimeError, "Bad Request"):
            send_report(reject, "test", None, "123")
        with self.assertRaisesRegex(RuntimeError, "no response"):
            send_report(lambda *_: None, "test", None, "123")

    def test_missing_chat_or_empty_text(self):
        with self.assertRaisesRegex(RuntimeError, "CHAT_ID"):
            send_report(lambda *_: {"ok": True}, "test", None, "")
        with self.assertRaises(ValueError):
            split_report("   ")


if __name__ == "__main__":
    unittest.main()
