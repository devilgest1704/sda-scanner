import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import whale_scanner


def _response(status, payload=None):
    response = Mock(status_code=status, text=f"HTTP {status}")
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status}", response=response)
    return response


class WhaleScannerTransientTests(unittest.TestCase):
    @patch("whale_scanner.time.sleep")
    @patch("whale_scanner.requests.get")
    def test_http_521_retries_and_recovers_on_third_attempt(self, get, sleep):
        get.side_effect = [
            _response(521), _response(521), _response(200, [{"id": 1}])]

        self.assertEqual(whale_scanner.fetch_supabase(), [{"id": 1}])
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])

    @patch("whale_scanner.time.sleep")
    @patch("whale_scanner.requests.get")
    def test_http_520_521_and_530_are_transient(self, get, sleep):
        for status in (520, 521, 530):
            with self.subTest(status=status):
                get.reset_mock()
                sleep.reset_mock()
                get.return_value = _response(status)
                with self.assertRaises(whale_scanner.TransientSupabaseError) as raised:
                    whale_scanner.fetch_supabase()
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(get.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])

    @patch("whale_scanner.time.sleep")
    @patch("whale_scanner.requests.get")
    def test_http_429_uses_bounded_backoff(self, get, sleep):
        get.return_value = _response(429)
        with self.assertRaises(whale_scanner.TransientSupabaseError):
            whale_scanner.fetch_supabase()
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])

    @patch("whale_scanner.time.sleep")
    @patch("whale_scanner.requests.get")
    def test_timeout_retries_and_recovers(self, get, sleep):
        get.side_effect = [requests.Timeout("slow"), _response(200, [])]
        self.assertEqual(whale_scanner.fetch_supabase(), [])
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5)

    @patch("whale_scanner.time.sleep")
    @patch("whale_scanner.requests.get")
    def test_http_400_is_not_retried(self, get, sleep):
        get.return_value = _response(400)
        with self.assertRaises(whale_scanner.SupabaseSourceError) as raised:
            whale_scanner.fetch_supabase()
        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(raised.exception.transient)
        get.assert_called_once()
        sleep.assert_not_called()

    def test_failure_preserves_last_good_snapshot_and_alert_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "whale_state.json")
            data_path = Path(directory, "whale_data.json")
            old_success = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            state_path.write_text(json.dumps({
                "updated_at": old_success,
                "last_successful_whale_at": old_success,
                "whale_source_health": "HEALTHY",
            }), encoding="utf-8")
            data_path.write_text('{"token": {"whale_buy_volume": 42}}', encoding="utf-8")
            error = whale_scanner.TransientSupabaseError(
                "temporary 521", status_code=521, attempts=3, transient=True)
            now = datetime.now(timezone.utc)
            with patch.object(whale_scanner, "STATE_FILE", str(state_path)):
                state, should_alert = whale_scanner._record_source_unavailable(error, now)
                self.assertTrue(should_alert)
                self.assertEqual(state["updated_at"], old_success)
                self.assertEqual(state["last_successful_whale_at"], old_success)
                self.assertEqual(state["whale_source_health"], "SOURCE_UNAVAILABLE")
                self.assertEqual(state["last_error_http_status"], 521)
                state, should_alert = whale_scanner._record_source_unavailable(
                    error, now + timedelta(seconds=60))
                self.assertFalse(should_alert)
                self.assertEqual(data_path.read_text(encoding="utf-8"),
                                 '{"token": {"whale_buy_volume": 42}}')

    def test_transient_error_logs_traceback_but_sends_concise_alert_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "whale_state.json")
            data_path = Path(directory, "whale_data.json")
            history_path = Path(directory, "whale_history.json")
            meta_path = Path(directory, "token_metadata.json")
            prior = datetime.now(timezone.utc).isoformat()
            state_path.write_text(json.dumps({"updated_at": prior}), encoding="utf-8")
            data_path.write_text('{"prior":"snapshot"}', encoding="utf-8")
            error = whale_scanner.TransientSupabaseError(
                "temporary 521", status_code=521, attempts=3, transient=True)
            with patch.object(whale_scanner, "STATE_FILE", str(state_path)), \
                 patch.object(whale_scanner, "DATA_FILE", str(data_path)), \
                 patch.object(whale_scanner, "HISTORY_FILE", str(history_path)), \
                 patch.object(whale_scanner, "META_FILE", str(meta_path)), \
                 patch.object(whale_scanner, "TOKEN", "test-token"), \
                 patch.object(whale_scanner, "CHAT_ID", "test-chat"), \
                 patch.object(whale_scanner, "fetch_supabase", side_effect=error), \
                 patch.object(whale_scanner, "_send_whale_alert") as alert:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        whale_scanner.run()
                self.assertEqual(raised.exception.code, 75)
                self.assertIn("Traceback (most recent call last)", stderr.getvalue())
                alert.assert_called_once()
                self.assertIn("BUY data degraded", alert.call_args.args[0])
                self.assertNotIn("Traceback", alert.call_args.args[0])
                self.assertIn("REAL SELL protection: ACTIVE", alert.call_args.args[0])
                self.assertEqual(data_path.read_text(encoding="utf-8"), '{"prior":"snapshot"}')

    def test_successful_request_marks_source_fresh_and_sends_recovery_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory, "whale_state.json")
            data_path = Path(directory, "whale_data.json")
            history_path = Path(directory, "whale_history.json")
            meta_path = Path(directory, "token_metadata.json")
            state_path.write_text(json.dumps({
                "updated_at": "2026-11-13T00:00:00+00:00",
                "whale_source_health": "SOURCE_UNAVAILABLE",
                "last_whale_alert_at": "2026-11-13T00:01:00+00:00",
            }), encoding="utf-8")
            with patch.object(whale_scanner, "STATE_FILE", str(state_path)), \
                 patch.object(whale_scanner, "DATA_FILE", str(data_path)), \
                 patch.object(whale_scanner, "HISTORY_FILE", str(history_path)), \
                 patch.object(whale_scanner, "META_FILE", str(meta_path)), \
                 patch("whale_scanner.requests.get", return_value=_response(200, [])), \
                 patch.object(whale_scanner, "_send_whale_alert") as alert:
                whale_scanner.main()
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["whale_source_health"], "HEALTHY")
                self.assertEqual(state["data_source_health"]["whale"], "HEALTHY")
                self.assertEqual(state["updated_at"], state["last_successful_whale_at"])
                self.assertTrue(whale_scanner.ts(state["updated_at"]))
                alert.assert_called_once_with("✅ BUY data recovered\nWhale source healthy")
                whale_scanner.main()
                alert.assert_called_once()


class ScannerFailureIsolationTests(unittest.TestCase):
    def test_whale_failure_does_not_return_before_market_paper_or_bridge(self):
        source = Path("run_scanner_v18.sh").read_text(encoding="utf-8")
        start = source.index('if [ "$whale_rc" -eq 75 ]')
        end = source.index("# Keep a single writer", start)
        branch = source[start:end]
        self.assertNotIn("return 0", branch)
        self.assertIn("buy_data_available=false", branch)
        self.assertGreater(source.index("if ! run_timed_stage market_scanner"), end)
        paper = source.index("if ! run_timed_stage paper_engine")
        bridge = source.index("SDA_SCANNER_BUY_DATA_AVAILABLE=", paper)
        self.assertGreater(bridge, paper)

    def test_periodic_state_checkpoint_is_outside_the_scan_function(self):
        source = Path("run_scanner_v18.sh").read_text(encoding="utf-8")
        loop = source.index("while true; do")
        self.assertGreater(source.index("checkpoint_state", loop), loop)
        self.assertIn("run_one_scan", source[loop:])


if __name__ == "__main__":
    unittest.main()