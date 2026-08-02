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


def _mk_thread(client, user):
    return client.post("/api/ai/threads", headers=auth_headers(user)).json()["id"]


def test_ai_chat_persists_and_history_syncs(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    user = make_user(db_session, role="student")
    tid = _mk_thread(client, user)

    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "Hello AI"}], "thread_id": tid},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "AI reply"

    # Дельта сохранена: сообщение пользователя + ответ ассистента.
    hist = client.get(
        "/api/ai/history", params={"thread_id": tid}, headers=auth_headers(user)
    ).json()
    assert [(m["role"], m["content"]) for m in hist] == [
        ("user", "Hello AI"), ("assistant", "AI reply"),
    ]

    # Второй запрос со всей историей пишет только новую дельту (без дублей).
    client.post(
        "/api/ai/chat",
        json={"messages": [
            {"role": "user", "content": "Hello AI"},
            {"role": "assistant", "content": "AI reply"},
            {"role": "user", "content": "Second"},
        ], "thread_id": tid},
        headers=auth_headers(user),
    )
    hist = client.get(
        "/api/ai/history", params={"thread_id": tid}, headers=auth_headers(user)
    ).json()
    assert [m["content"] for m in hist] == ["Hello AI", "AI reply", "Second", "AI reply"]


def test_ai_history_class_thread_is_separate(client, db_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    teacher = make_user(db_session, role="teacher")
    user = make_user(db_session, role="student")
    # /ai/chat с class_id теперь проверяет членство в классе (см.
    # routers/ai.py::_check_class_access) — раньше любой class_id проходил
    # без проверки, здесь регистрируем реальный класс и вступаем в него.
    cls = client.post(
        "/api/classes/", json={"name": "Math"}, headers=auth_headers(teacher)
    ).json()
    client.post(
        "/api/classes/join-by-code", json={"code": cls["invite_code"]},
        headers=auth_headers(user),
    )
    tid = _mk_thread(client, user)

    client.post("/api/ai/chat",
                json={"messages": [{"role": "user", "content": "global"}], "thread_id": tid},
                headers=auth_headers(user))
    client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "in class"}], "class_id": cls["id"]},
                headers=auth_headers(user))

    g = client.get("/api/ai/history", params={"thread_id": tid}, headers=auth_headers(user)).json()
    c = client.get("/api/ai/history", params={"class_id": cls["id"]}, headers=auth_headers(user)).json()
    assert [m["content"] for m in g] == ["global", "AI reply"]
    assert [m["content"] for m in c] == ["in class", "AI reply"]


def test_ai_chat_class_id_requires_membership(client, db_session, monkeypatch):
    """BUG FIX: /ai/chat раньше принимал произвольный/чужой class_id без
    проверки членства — писал историю и usage-логи под ним. Теперь и
    несуществующий класс, и класс без членства должны отбиваться."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)
    teacher = make_user(db_session, role="teacher")
    outsider = make_user(db_session, role="student")

    # Несуществующий класс.
    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "class_id": 999999},
        headers=auth_headers(outsider),
    )
    assert resp.status_code == 404

    # Существующий класс, но пользователь не состоит в нём.
    cls = client.post(
        "/api/classes/", json={"name": "Physics"}, headers=auth_headers(teacher)
    ).json()
    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "class_id": cls["id"]},
        headers=auth_headers(outsider),
    )
    assert resp.status_code == 403


def test_ai_history_import_only_when_empty_and_clear(client, db_session):
    user = make_user(db_session, role="student")
    tid = _mk_thread(client, user)

    imported = client.post(
        "/api/ai/history/import",
        json={"thread_id": tid, "messages": [
            {"role": "user", "content": "old q"},
            {"role": "assistant", "content": "old a"},
        ]},
        headers=auth_headers(user),
    ).json()
    assert [m["content"] for m in imported] == ["old q", "old a"]

    # Повторный импорт в непустой тред ничего не добавляет (идемпотентно).
    again = client.post(
        "/api/ai/history/import",
        json={"thread_id": tid, "messages": [{"role": "user", "content": "should be ignored"}]},
        headers=auth_headers(user),
    ).json()
    assert [m["content"] for m in again] == ["old q", "old a"]

    # Очистка треда.
    client.delete("/api/ai/history", params={"thread_id": tid}, headers=auth_headers(user))
    assert client.get(
        "/api/ai/history", params={"thread_id": tid}, headers=auth_headers(user)
    ).json() == []


def test_ai_history_isolated_per_user(client, db_session):
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")
    tid_a = _mk_thread(client, a)
    tid_b = _mk_thread(client, b)
    client.post("/api/ai/history/import",
                json={"thread_id": tid_a, "messages": [{"role": "user", "content": "a-secret"}]},
                headers=auth_headers(a))
    assert client.get(
        "/api/ai/history", params={"thread_id": tid_b}, headers=auth_headers(b)
    ).json() == []


# ── Notification state ────────────────────────────────────────────────────────

def test_notification_state_upsert_and_sync(client, db_session):
    user = make_user(db_session, role="student")

    # Изначально пусто.
    assert client.get("/api/notifications/state", headers=auth_headers(user)).json() == []

    # Пометить прочитанным.
    resp = client.post("/api/notifications/state",
                       json={"notif_key": "grade:12", "read": True},
                       headers=auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"notif_key": "grade:12", "read": True, "dismissed": False}

    # Upsert: добавить dismissed, read не трогаем.
    resp = client.post("/api/notifications/state",
                       json={"notif_key": "grade:12", "dismissed": True},
                       headers=auth_headers(user))
    assert resp.json() == {"notif_key": "grade:12", "read": True, "dismissed": True}

    state = client.get("/api/notifications/state", headers=auth_headers(user)).json()
    assert state == [{"notif_key": "grade:12", "read": True, "dismissed": True}]


def test_notification_read_all(client, db_session):
    user = make_user(db_session, role="student")
    client.post("/api/notifications/read-all",
                json={"keys": ["assignment:1", "deadline:1", "grade:3"]},
                headers=auth_headers(user))
    state = {s["notif_key"]: s["read"] for s in
             client.get("/api/notifications/state", headers=auth_headers(user)).json()}
    assert state == {"assignment:1": True, "deadline:1": True, "grade:3": True}


def test_notification_state_isolated_per_user(client, db_session):
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")
    client.post("/api/notifications/state", json={"notif_key": "grade:1", "read": True},
                headers=auth_headers(a))
    assert client.get("/api/notifications/state", headers=auth_headers(b)).json() == []
