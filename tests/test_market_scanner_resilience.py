import json
import runpy
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]


def _response(status, payload=None):
    response = Mock(status_code=status, text=f"HTTP {status}", headers={})
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
    monkeypatch.setattr("tools.market_source_http.uniform", lambda *_: 0)
    Path("market_data.json").write_text(
        '{"updated_at":"2026-10-07T15:05:14+00:00","tokens":{}}', encoding="utf-8")


def _run_scanner():
    runpy.run_path(str(ROOT / "market_scanner.py"))


def _fresh_trade(stamp=None):
    return {"token_address":"0x" + "a" * 40, "tx_hash":"trade-1", "price_in_sda":1,
            "volume_in_sda":1, "tx_timestamp":stamp or datetime.now(timezone.utc).isoformat(),
            "tx_type":"buy"}


@pytest.mark.parametrize("status", [521, 530, 540, None])
def test_transient_error_retries_and_marks_source_healthy(monkeypatch, tmp_path, status):
    _prepare(monkeypatch, tmp_path)
    dns_error = requests.ConnectionError("private request details")
    dns_error.__cause__ = socket.gaierror(-2, "private DNS details")
    get = Mock(side_effect=[_response(status) if status else dns_error, _response(200, [_fresh_trade()])])
    monkeypatch.setattr("requests.get", get)
    with patch("time.sleep") as sleep:
        _run_scanner()

    assert get.call_count == 2
    sleep.assert_called_once_with(5)
    state = json.loads(Path("market_source_state.json").read_text(encoding="utf-8"))
    assert state["source_health"] == "HEALTHY"
    assert state["last_successful_market_at"] == state["updated_at"]


@pytest.mark.parametrize("rows", [[], [_fresh_trade("2026-10-07T15:05:14+00:00")]])
def test_empty_or_stale_response_preserves_snapshot_and_blocks_buy(monkeypatch, tmp_path, rows):
    _prepare(monkeypatch, tmp_path)
    previous = Path("market_data.json").read_bytes()
    monkeypatch.setattr("requests.get", Mock(return_value=_response(200, rows)))
    with pytest.raises(SystemExit) as raised:
        _run_scanner()
    assert raised.value.code == 75
    assert Path("market_data.json").read_bytes() == previous
    assert json.loads(Path("market_source_state.json").read_text())["source_health"] == "SOURCE_UNAVAILABLE"


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
    assert "REAL SELL protection: UNKNOWN" in text
    assert "Traceback" not in text


def test_market_failure_disables_buy_and_publishes_source_health():
    runner = (ROOT / "run_scanner_v18.sh").read_text(encoding="utf-8")
    start = runner.index("if ! run_timed_stage market_scanner")
    end = runner.index("if ! run_timed_stage technical_analysis", start)
    market_branch = runner[start:end]
    assert "buy_data_available=false" in market_branch
    assert "market_source_state.json" in runner
    bridge = runner.index('from real_paper_bridge import reconcile')
    real_buy = runner.index("if ! run_timed_stage real_buy_pipeline")
    paper = runner.index("if ! run_timed_stage paper_engine")
    assert bridge < real_buy < paper
    assert 'SDA_SCANNER_BUY_DATA_AVAILABLE="$buy_data_available"' in runner
    assert "python -m tools.real_buy_pipeline" in runner


def test_diagnostics_and_retry_after_survive_to_next_scan(monkeypatch, tmp_path, capsys):
    _prepare(monkeypatch, tmp_path)
    response = _response(429)
    response.headers = {"Retry-After": "120", "Server": "cloudflare", "CF-Ray": "abc-PRG",
                        "Proxy-Status": 'gateway; error=dns_error; details="private-token"'}
    response.text = "Error code 1016 with private-body"
    get = Mock(side_effect=[response, _response(200, [_fresh_trade()])])
    monkeypatch.setattr("requests.get", get)
    with patch("time.sleep") as sleep:
        for _ in range(2):
            with pytest.raises(SystemExit) as failed:
                _run_scanner()
            assert failed.value.code == 75
        get.assert_called_once()
        sleep.assert_not_called()
    path = Path("market_source_state.json")
    state = json.loads(path.read_text())
    detail = state["last_error_diagnostic"]
    assert detail["http_status"] == 429
    assert detail["timestamp"]
    assert detail["server"] == "cloudflare"
    assert detail["cf_ray"] == "abc-PRG"
    assert detail["proxy_status"] == "gateway; error=dns_error"
    assert detail["response_detail"] == "edge error code 1016"
    assert "private-" not in path.read_text() + capsys.readouterr().out
    detail["retry_not_before"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    _run_scanner()
    assert get.call_count == 2
    assert json.loads(path.read_text())["last_error_diagnostic"] is None


def test_retry_after_http_date_and_jitter(monkeypatch, tmp_path):
    from email.utils import format_datetime

    _prepare(monkeypatch, tmp_path)
    response = _response(503)
    response.headers = {"Retry-After": format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))}
    monkeypatch.setattr("tools.market_source_http.uniform", lambda low, high: high)
    monkeypatch.setattr("requests.get", Mock(side_effect=[
        response, _response(530), _response(200, [_fresh_trade()]),
    ]))
    with patch("time.sleep") as sleep:
        _run_scanner()
    delays = [call.args[0] for call in sleep.call_args_list]
    assert 18 < delays[0] <= 20
    assert delays[1] == 12.5


def test_primary_outage_blocks_buy_then_recovers_without_restart(monkeypatch, tmp_path):
    from tools import real_buy_pipeline as buy

    _prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    buy_state = tmp_path / "real_buy_signal_state.json"
    buy.save_state(buy.initial_state(), buy_state)
    market = tmp_path / "market_analysis.json"
    market.write_text('{"tokens":{}}', encoding="utf-8")
    get = Mock(side_effect=[_response(530)] * 3 + [_response(200, [_fresh_trade()])])
    monkeypatch.setattr("requests.get", get)
    monkeypatch.setattr(buy.real_trader, "load_state", lambda: {"positions": {}})
    with patch.object(buy.real_trader, "execute_buy", side_effect=AssertionError("no broadcast")) as execution:
        with patch("time.sleep"), pytest.raises(SystemExit):
            _run_scanner()
        monkeypatch.setenv("SDA_SCANNER_BUY_DATA_AVAILABLE", "false")
        assert buy.run_scan(market, execute=False, state_path=buy_state)["status"] == "market_data_degraded"
        _run_scanner()
        monkeypatch.setenv("SDA_SCANNER_BUY_DATA_AVAILABLE", "true")
        assert buy.run_scan(market, execute=False, state_path=buy_state)["status"] == "ok"
        assert buy.load_state(buy_state)[0]["market_data_status"] == "OK"
        execution.assert_not_called()