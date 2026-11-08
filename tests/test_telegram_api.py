import telegram_dashboard


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(payload)

    def json(self):
        return self._payload


def test_telegram_api_returns_error_body_without_logging_bot_token(monkeypatch, capsys):
    token = "TEST_BOT_TOKEN_DO_NOT_LOG"
    monkeypatch.setenv("TELEGRAM_TOKEN", token)
    monkeypatch.setattr(
        telegram_dashboard.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {"ok": False, "error_code": 400, "description": "Bad Request: query is too old"},
            400,
        ),
    )

    result = telegram_dashboard.api("answerCallbackQuery", {"callback_query_id": "cb-1"})

    output = capsys.readouterr().out
    assert result["ok"] is False
    assert result["description"] == "Bad Request: query is too old"
    assert "query is too old" in output
    assert token not in output


def test_telegram_api_uses_bounded_timeout_and_returns_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    seen = {}

    def fake_post(url, json, timeout):
        seen.update({"url": url, "timeout": timeout, "json": json})
        return FakeResponse({"ok": True, "result": True})

    monkeypatch.setattr(telegram_dashboard.requests, "post", fake_post)
    result = telegram_dashboard.api("answerCallbackQuery", {"callback_query_id": "cb-2"})

    assert result["ok"] is True
    assert seen["timeout"] == (2, 8)
    assert seen["json"]["callback_query_id"] == "cb-2"


def test_edit_falls_back_to_new_message_when_edit_fails(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    calls = []

    def fake_api(method, payload):
        calls.append((method, payload))
        return {"ok": method == "sendMessage", "description": "Bad Request: message to edit not found"}

    monkeypatch.setattr(telegram_dashboard, "api", fake_api)
    result = telegram_dashboard.edit(123, 10, "REAL menu", {"inline_keyboard": []})

    assert result["ok"] is False
    assert [method for method, _ in calls] == ["editMessageText", "sendMessage"]
    assert calls[1][1]["reply_markup"] == {"inline_keyboard": []}
