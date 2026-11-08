from fastapi.testclient import TestClient

import api.index as telegram_api


def test_webhook_does_not_load_remote_menu_state(monkeypatch):
    received = {}

    def forbidden_load(*args, **kwargs):
        raise AssertionError("webhook must not perform remote menu-state lookup")

    def handle_update(update, state):
        received["update"] = update
        received["state"] = state

    monkeypatch.setattr(telegram_api.dashboard, "load", forbidden_load)
    monkeypatch.setattr(telegram_api.dashboard, "handle_update", handle_update)

    response = TestClient(telegram_api.app).post(
        "/api/telegram",
        json={"update_id": 12, "callback_query": {"id": "cb-12", "data": "MAIN"}},
    )

    assert response.status_code == 200
    assert response.text == "ok"
    assert received["update"]["callback_query"]["data"] == "MAIN"
    assert received["state"] == {"offset": 0}


def test_webhook_returns_ok_and_sends_navigation_on_handler_exception(monkeypatch):
    sent = {}

    def broken_handler(update, state):
        raise RuntimeError("dashboard report unavailable")

    def send(text, reply_markup=None, chat_id=None):
        sent.update({"text": text, "reply_markup": reply_markup, "chat_id": chat_id})

    monkeypatch.setattr(telegram_api.dashboard, "handle_update", broken_handler)
    monkeypatch.setattr(telegram_api.dashboard, "send", send)
    monkeypatch.setattr(
        telegram_api.dashboard,
        "menu_keyboard",
        lambda: {"inline_keyboard": [[{"text": "Main", "callback_data": "MAIN"}]]},
    )

    response = TestClient(telegram_api.app).post(
        "/api/telegram",
        json={"update_id": 13, "callback_query": {
            "id": "cb-13", "data": "REAL_BOT",
            "message": {"chat": {"id": 12345}, "message_id": 20},
        }},
    )

    assert response.status_code == 200
    assert sent["chat_id"] == 12345
    assert "temporarily unavailable" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "MAIN"

def test_webhook_health_reports_only_safe_telegram_status(monkeypatch):
    monkeypatch.setattr(
        telegram_api.dashboard,
        "api",
        lambda method, payload: {
            "ok": True,
            "result": {
                "url": "https://sda-scanner.vercel.app/api/telegram",
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": None,
                "allowed_updates": ["message", "callback_query"],
                "max_connections": 40,
            },
        },
    )

    response = TestClient(telegram_api.app).get("/api/telegram/health")

    assert response.status_code == 200
    data = response.json()
    assert data["telegram_api"] == "ok"
    assert data["webhook_configured"] is True
    assert data["webhook_url_matches_production"] is True
    assert data["pending_update_count"] == 0
    assert "url" not in data
