"""Синхронизация между приложением и сайтом: серверная история чатов с ИИ и
серверное состояние уведомлений (прочитано/скрыто)."""
import os

from models import AiMessage
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers


# ── AI history ────────────────────────────────────────────────────────────────

class _FakeResp:
    is_success = True
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "AI reply"}}], "usage": {}}


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp()


def test_ai_chat_persists_and_history_syncs(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")

    resp = client.post(
        "/ai/chat",
        json={"messages": [{"role": "user", "content": "Hello AI"}]},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "AI reply"

    # Дельта сохранена: сообщение пользователя + ответ ассистента.
    hist = client.get("/ai/history", headers=auth_headers(user)).json()
    assert [(m["role"], m["content"]) for m in hist] == [
        ("user", "Hello AI"), ("assistant", "AI reply"),
    ]

    # Второй запрос со всей историей пишет только новую дельту (без дублей).
    client.post(
        "/ai/chat",
        json={"messages": [
            {"role": "user", "content": "Hello AI"},
            {"role": "assistant", "content": "AI reply"},
            {"role": "user", "content": "Second"},
        ]},
        headers=auth_headers(user),
    )
    hist = client.get("/ai/history", headers=auth_headers(user)).json()
    assert [m["content"] for m in hist] == ["Hello AI", "AI reply", "Second", "AI reply"]


def test_ai_history_class_thread_is_separate(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")

    client.post("/ai/chat", json={"messages": [{"role": "user", "content": "global"}]},
                headers=auth_headers(user))
    client.post("/ai/chat", json={"messages": [{"role": "user", "content": "in class"}], "class_id": 7},
                headers=auth_headers(user))

    g = client.get("/ai/history", headers=auth_headers(user)).json()
    c = client.get("/ai/history", params={"class_id": 7}, headers=auth_headers(user)).json()
    assert [m["content"] for m in g] == ["global", "AI reply"]
    assert [m["content"] for m in c] == ["in class", "AI reply"]


def test_ai_history_import_only_when_empty_and_clear(client, db_session):
    user = make_user(db_session, role="student")

    imported = client.post(
        "/ai/history/import",
        json={"messages": [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
        ]},
        headers=auth_headers(user),
    ).json()
    assert [m["content"] for m in imported] == ["old q", "old a"]

    # Повторный импорт в непустой тред ничего не добавляет (идемпотентно).
    again = client.post(
        "/ai/history/import",
        json={"messages": [{"role": "user", "content": "should be ignored"}]},
        headers=auth_headers(user),
    ).json()
    assert [m["content"] for m in again] == ["old q", "old a"]

    # Очистка треда.
    client.delete("/ai/history", headers=auth_headers(user))
    assert client.get("/ai/history", headers=auth_headers(user)).json() == []


def test_ai_history_isolated_per_user(client, db_session):
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")
    client.post("/ai/history/import",
                json={"messages": [{"role": "user", "content": "a-secret"}]},
                headers=auth_headers(a))
    assert client.get("/ai/history", headers=auth_headers(b)).json() == []


# ── Notification state ────────────────────────────────────────────────────────

def test_notification_state_upsert_and_sync(client, db_session):
    user = make_user(db_session, role="student")

    # Изначально пусто.
    assert client.get("/notifications/state", headers=auth_headers(user)).json() == []

    # Пометить прочитанным.
    resp = client.post("/notifications/state",
                       json={"notif_key": "grade:12", "read": True},
                       headers=auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"notif_key": "grade:12", "read": True, "dismissed": False}

    # Upsert: добавить dismissed, read не трогаем.
    resp = client.post("/notifications/state",
                       json={"notif_key": "grade:12", "dismissed": True},
                       headers=auth_headers(user))
    assert resp.json() == {"notif_key": "grade:12", "read": True, "dismissed": True}

    state = client.get("/notifications/state", headers=auth_headers(user)).json()
    assert state == [{"notif_key": "grade:12", "read": True, "dismissed": True}]


def test_notification_read_all(client, db_session):
    user = make_user(db_session, role="student")
    client.post("/notifications/read-all",
                json={"keys": ["assignment:1", "deadline:1", "grade:3"]},
                headers=auth_headers(user))
    state = {s["notif_key"]: s["read"] for s in
             client.get("/notifications/state", headers=auth_headers(user)).json()}
    assert state == {"assignment:1": True, "deadline:1": True, "grade:3": True}


def test_notification_state_isolated_per_user(client, db_session):
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")
    client.post("/notifications/state", json={"notif_key": "grade:1", "read": True},
                headers=auth_headers(a))
    assert client.get("/notifications/state", headers=auth_headers(b)).json() == []
