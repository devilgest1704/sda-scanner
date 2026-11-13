import json
import runpy
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]


def _response(status, payload=None):
    response = Mock(status_code=status, text=f"HTTP {status}")
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status}", response=response)
    return response


def _prepare(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARKET_SUPABASE_RETRIES", "3")
    monkeypatch.setenv("MARKET_SUPABASE_RETRY_BASE_SECONDS", "5")
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("CHAT_ID", raising=False)
    Path("market_data.json").write_text(
        '{"updated_at":"2026-10-07T15:05:14+00:00","tokens":{}}', encoding="utf-8")


def _run_scanner():
    runpy.run_path(str(ROOT / "market_scanner.py"))


def test_transient_http_error_retries_and_marks_source_healthy(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    get = Mock(side_effect=[_response(521), _response(200, [])])
    monkeypatch.setattr("requests.get", get)
    with patch("time.sleep") as sleep:
        _run_scanner()

    assert get.call_count == 2
    sleep.assert_called_once_with(5)
    state = json.loads(Path("market_source_state.json").read_text(encoding="utf-8"))
    assert state["source_health"] == "HEALTHY"
    assert state["last_successful_market_at"] == state["updated_at"]


def test_exhausted_transient_error_preserves_data_and_exits_nonzero(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    previous = Path("market_data.json").read_text(encoding="utf-8")
    get = Mock(return_value=_response(521))
    monkeypatch.setattr("requests.get", get)
    with patch("time.sleep") as sleep:
        with pytest.raises(SystemExit) as raised:
            _run_scanner()

    assert raised.value.code == 75
    assert get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [5, 10]
    assert Path("market_data.json").read_text(encoding="utf-8") == previous
    state = json.loads(Path("market_source_state.json").read_text(encoding="utf-8"))
    assert state["source_health"] == "SOURCE_UNAVAILABLE"
    assert state["last_error_http_status"] == 521


def test_http_400_fails_without_retry(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    get = Mock(return_value=_response(400))
    monkeypatch.setattr("requests.get", get)
    with patch("time.sleep") as sleep:
        with pytest.raises(SystemExit) as raised:
            _run_scanner()

    assert raised.value.code == 75
    get.assert_called_once()
    sleep.assert_not_called()
    state = json.loads(Path("market_source_state.json").read_text(encoding="utf-8"))
    assert state["last_error_http_status"] == 400


def test_market_source_alert_is_concise_and_rate_limited(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("CHAT_ID", "test-chat")
    get = Mock(side_effect=[_response(521) for _ in range(6)])
    post = Mock()
    monkeypatch.setattr("requests.get", get)
    monkeypatch.setattr("requests.post", post)

    with patch("time.sleep"):
        with pytest.raises(SystemExit):
            _run_scanner()
        with pytest.raises(SystemExit):
            _run_scanner()

    post.assert_called_once()
    text = post.call_args.kwargs["json"]["text"]
    assert "BUY data degraded" in text
    assert "HTTP 521" in text
    assert "REAL SELL protection: ACTIVE" in text
    assert "Traceback" not in text


def test_market_failure_disables_buy_and_publishes_source_health():
    runner = (ROOT / "run_scanner_v18.sh").read_text(encoding="utf-8")
    start = runner.index("if ! run_timed_stage market_scanner")
    end = runner.index("if ! run_timed_stage technical_analysis", start)
    market_branch = runner[start:end]
    assert "buy_data_available=false" in market_branch
    assert "market_source_state.json" in runner
    paper = runner.index("if ! run_timed_stage paper_engine")
    bridge = runner.index('SDA_SCANNER_BUY_DATA_AVAILABLE="$buy_data_available"', paper)
    assert bridge > paper