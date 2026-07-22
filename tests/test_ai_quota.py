"""Дневной лимит сообщений ИИ на пользователя (services/ai_quota.py)."""
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers
from tests.test_sync import _FakeClient


def _new_thread(client, user):
    return client.post("/ai/threads", headers=auth_headers(user)).json()["id"]


def _send(client, user, thread_id=None):
    if thread_id is None:
        thread_id = _new_thread(client, user)
    return client.post(
        "/ai/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "thread_id": thread_id},
        headers=auth_headers(user),
    )


def test_daily_limit_blocks_after_quota(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_MESSAGE_LIMIT", "3")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")

    for i in range(3):
        assert _send(client, user).status_code == 200, f"запрос {i + 1}"

    blocked = _send(client, user)
    assert blocked.status_code == 429
    assert "лимит" in blocked.json()["detail"].lower()

    limits = client.get("/ai/limits", headers=auth_headers(user)).json()
    assert limits["limit"] == 3
    assert limits["used"] == 3
    assert limits["remaining"] == 0
    assert limits["unlimited"] is False


def test_limit_is_per_user(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_MESSAGE_LIMIT", "1")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")

    assert _send(client, a).status_code == 200
    assert _send(client, a).status_code == 429
    # Лимит другого пользователя не тронут.
    assert _send(client, b).status_code == 200


def test_ai_unlimited_user_is_exempt(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_MESSAGE_LIMIT", "1")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")
    user.ai_unlimited = True
    db_session.commit()

    assert _send(client, user).status_code == 200
    assert _send(client, user).status_code == 200

    limits = client.get("/ai/limits", headers=auth_headers(user)).json()
    assert limits["unlimited"] is True
    assert limits["remaining"] is None


def test_chat_response_carries_quota(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AI_DAILY_MESSAGE_LIMIT", "50")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")

    quota = _send(client, user).json()["quota"]
    assert quota["limit"] == 50
    assert quota["used"] == 1
    assert quota["remaining"] == 49
