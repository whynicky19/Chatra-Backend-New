"""Мульти-чаты (треды) главного ассистента: CRUD, изоляция, чат-раунд-трип.

Треды есть ТОЛЬКО у главного ассистента (class_id IS NULL). Путь репетитора
класса (class_id задан) тредов не знает и должен работать как раньше."""
import time

from models import AiMessage, AiThread
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers
from tests.test_sync import _FakeClient


def _use_fake_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeClient)


def _mk_thread(client, user):
    resp = client.post("/api/ai/threads", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── CRUD ────────────────────────────────────────────────────────────────────────

def test_create_thread_defaults(client, db_session):
    user = make_user(db_session, role="student")
    t = _mk_thread(client, user)
    assert t["title"] == "Новый чат"
    assert t["pinned"] is False
    assert "created_at" in t and "updated_at" in t


def test_list_threads_ordering(client, db_session, monkeypatch):
    user = make_user(db_session, role="student")
    # Три треда с разным updated_at (создаём по очереди с паузой).
    a = _mk_thread(client, user)
    time.sleep(0.01)
    b = _mk_thread(client, user)
    time.sleep(0.01)
    c = _mk_thread(client, user)

    # Закрепляем самый старый — он должен всплыть наверх.
    client.patch(f"/api/ai/threads/{a['id']}", json={"pinned": True},
                 headers=auth_headers(user))

    listed = client.get("/api/ai/threads", headers=auth_headers(user)).json()
    ids = [t["id"] for t in listed]
    # Закреплённый первым; среди остальных — по updated_at desc (c новее b).
    assert ids[0] == a["id"]
    assert ids[1:] == [c["id"], b["id"]]


def test_patch_renames_and_pins(client, db_session):
    user = make_user(db_session, role="student")
    t = _mk_thread(client, user)

    renamed = client.patch(f"/api/ai/threads/{t['id']}", json={"title": "Матан"},
                           headers=auth_headers(user)).json()
    assert renamed["title"] == "Матан"
    assert renamed["pinned"] is False

    pinned = client.patch(f"/api/ai/threads/{t['id']}", json={"pinned": True},
                          headers=auth_headers(user)).json()
    assert pinned["pinned"] is True
    assert pinned["title"] == "Матан"  # не переданное поле не тронуто


def test_delete_thread_removes_messages(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")
    t = _mk_thread(client, user)

    client.post("/api/ai/chat",
                json={"messages": [{"role": "user", "content": "hi"}], "thread_id": t["id"]},
                headers=auth_headers(user))
    assert db_session.query(AiMessage).filter(AiMessage.thread_id == t["id"]).count() == 2

    resp = client.delete(f"/api/ai/threads/{t['id']}", headers=auth_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    db_session.expire_all()
    assert db_session.query(AiThread).filter(AiThread.id == t["id"]).first() is None
    assert db_session.query(AiMessage).filter(AiMessage.thread_id == t["id"]).count() == 0


# ── Изоляция по пользователю (404, не раскрываем существование чужих) ────────────

def test_cannot_access_other_users_thread(client, db_session):
    a = make_user(db_session, role="student")
    b = make_user(db_session, role="student")
    t = _mk_thread(client, a)

    assert client.get("/api/ai/history", params={"thread_id": t["id"]},
                      headers=auth_headers(b)).status_code == 404
    assert client.patch(f"/api/ai/threads/{t['id']}", json={"title": "hijack"},
                        headers=auth_headers(b)).status_code == 404
    assert client.delete(f"/api/ai/threads/{t['id']}",
                         headers=auth_headers(b)).status_code == 404
    # Тред a не пострадал.
    assert client.get("/api/ai/threads", headers=auth_headers(a)).json()[0]["id"] == t["id"]


# ── Чат-раунд-трип главного ассистента ──────────────────────────────────────────

def test_chat_requires_thread_id_for_main_assistant(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")
    resp = client.post("/api/ai/chat",
                       json={"messages": [{"role": "user", "content": "hi"}]},
                       headers=auth_headers(user))
    assert resp.status_code == 400


def test_chat_unknown_thread_is_404(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")
    resp = client.post("/api/ai/chat",
                       json={"messages": [{"role": "user", "content": "hi"}], "thread_id": 999999},
                       headers=auth_headers(user))
    assert resp.status_code == 404


def test_main_chat_roundtrip_persists_and_bumps_updated_at(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")
    t = _mk_thread(client, user)
    created_updated = t["updated_at"]

    time.sleep(0.01)
    resp = client.post("/api/ai/chat",
                       json={"messages": [{"role": "user", "content": "Реши интеграл"}],
                             "thread_id": t["id"]},
                       headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "AI reply"
    # Заголовок автогенерён на первом обмене (FakeClient возвращает "AI reply").
    assert body["thread_title"] == "AI reply"

    # Сообщения привязаны к треду.
    hist = client.get("/api/ai/history", params={"thread_id": t["id"]},
                      headers=auth_headers(user)).json()
    assert [(m["role"], m["content"]) for m in hist] == [
        ("user", "Реши интеграл"), ("assistant", "AI reply"),
    ]

    # updated_at сдвинулся вперёд относительно момента создания.
    after = client.get("/api/ai/threads", headers=auth_headers(user)).json()[0]
    assert after["updated_at"] > created_updated
    assert after["title"] == "AI reply"


def test_title_only_generated_on_first_exchange(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")
    t = _mk_thread(client, user)

    client.post("/api/ai/chat",
                json={"messages": [{"role": "user", "content": "first"}], "thread_id": t["id"]},
                headers=auth_headers(user))
    # Переименуем вручную, затем второй обмен НЕ должен перетереть заголовок.
    client.patch(f"/api/ai/threads/{t['id']}", json={"title": "Мой чат"},
                 headers=auth_headers(user))
    resp = client.post("/api/ai/chat",
                       json={"messages": [{"role": "user", "content": "second"}],
                             "thread_id": t["id"]},
                       headers=auth_headers(user))
    assert resp.json()["thread_title"] == "Мой чат"


# ── Путь репетитора класса неизменён (тредов не знает) ───────────────────────────

def test_class_tutor_path_unaffected(client, db_session, monkeypatch):
    _use_fake_openai(monkeypatch)
    user = make_user(db_session, role="student")

    # Без thread_id, с class_id — работает как раньше.
    resp = client.post("/api/ai/chat",
                       json={"messages": [{"role": "user", "content": "в классе"}],
                             "class_id": 42},
                       headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "AI reply"
    assert resp.json()["thread_title"] is None

    # История класса читается по class_id, thread_id не нужен.
    hist = client.get("/api/ai/history", params={"class_id": 42},
                      headers=auth_headers(user)).json()
    assert [m["content"] for m in hist] == ["в классе", "AI reply"]

    # Сохранённые сообщения класса имеют thread_id = NULL.
    rows = db_session.query(AiMessage).filter(AiMessage.class_id == 42).all()
    assert rows and all(r.thread_id is None for r in rows)

    # thread_id, присланный на путь класса, игнорируется (не валидируется).
    resp2 = client.post("/api/ai/chat",
                        json={"messages": [{"role": "user", "content": "again"}],
                              "class_id": 42, "thread_id": 999999},
                        headers=auth_headers(user))
    assert resp2.status_code == 200
    assert resp2.json()["thread_title"] is None