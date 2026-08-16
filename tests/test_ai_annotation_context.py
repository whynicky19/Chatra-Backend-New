"""«Спросить AI» по выделенному фрагменту: сервер сам определяет, из какой
лекции и страницы взят текст, и кладёт это в системный контекст запроса.

Клиентской формулировке («объясни фрагмент из лекции X, стр. 12») не доверяем:
источник разбирается по annotation_id / lecture_id на сервере, поэтому чужую
лекцию в контекст этого предмета подсунуть нельзя.
"""
import json

import pytest

from crud import classes as crud_classes
from crud import posts as crud_posts
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers
from tests.test_sync import _FakeClient, _FakeResp


class _CapturingClient(_FakeClient):
    """Перехватывает payload, уходящий в OpenAI."""
    sent = None

    async def post(self, *a, **k):
        _CapturingClient.sent = k.get("json")
        return _FakeResp()


def _system_texts():
    return [m["content"] for m in _CapturingClient.sent["messages"] if m["role"] == "system"]


def _setup(db):
    teacher = make_user(db, role="teacher")
    student = make_user(db)
    cls = crud_classes.create_class(db, "ООП", None, created_by=teacher.id)
    crud_classes.add_member(db, cls.id, student.id)
    lecture = crud_posts.create_new_post(
        db,
        title=f"[LECTURE][{cls.id}] Инкапсуляция",
        body=json.dumps({"content": "Инкапсуляция скрывает внутреннее состояние объекта."}),
        user_id=teacher.id,
    )
    return teacher, student, cls, lecture


@pytest.fixture(autouse=True)
def _fake_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _CapturingClient)
    _CapturingClient.sent = None


def test_ask_ai_about_saved_annotation_adds_lecture_and_page_context(client, db_session):
    _, student, cls, lecture = _setup(db_session)
    h = auth_headers(student)
    ann = client.post("/api/annotations", json={
        "lecture_id": lecture.id, "class_id": cls.id, "file_index": 0, "page": 12,
        "selected_text": "инкапсуляция скрывает внутреннее состояние",
        "start_offset": 10, "end_offset": 53, "color": "yellow",
    }, headers=h).json()

    resp = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "Объясни этот фрагмент"}],
        "class_id": cls.id,
        "annotation_id": ann["id"],
    }, headers=h)
    assert resp.status_code == 200, resp.text

    ctx = "\n".join(_system_texts())
    assert "Инкапсуляция" in ctx           # название лекции
    assert "страница 12" in ctx            # номер страницы
    assert f"id лекции: {lecture.id}" in ctx
    assert "инкапсуляция скрывает внутреннее состояние" in ctx


def test_ask_ai_about_unsaved_selection(client, db_session):
    """Спросить можно и не сохраняя выделение — тогда клиент шлёт сам фрагмент
    вместе с lecture_id."""
    _, student, cls, lecture = _setup(db_session)

    resp = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "Объясни этот фрагмент"}],
        "class_id": cls.id,
        "lecture_id": lecture.id,
        "lecture_page": 3,
        "quote": "внутреннее состояние объекта",
    }, headers=auth_headers(student))
    assert resp.status_code == 200

    ctx = "\n".join(_system_texts())
    assert "внутреннее состояние объекта" in ctx
    assert "страница 3" in ctx


def test_lecture_from_another_class_is_rejected(client, db_session):
    teacher, student, cls, _ = _setup(db_session)
    other = crud_classes.create_class(db_session, "Физика", None, created_by=teacher.id)
    crud_classes.add_member(db_session, other.id, student.id)
    foreign = crud_posts.create_new_post(
        db_session, title=f"[LECTURE][{other.id}] Кинематика",
        body=json.dumps({"content": "x"}), user_id=teacher.id,
    )

    resp = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "Объясни"}],
        "class_id": cls.id,
        "lecture_id": foreign.id,
        "quote": "фрагмент",
    }, headers=auth_headers(student))
    assert resp.status_code == 400


def test_foreign_annotation_is_not_readable(client, db_session):
    _, student, cls, lecture = _setup(db_session)
    other_student = make_user(db_session)
    crud_classes.add_member(db_session, cls.id, other_student.id)
    ann = client.post("/api/annotations", json={
        "lecture_id": lecture.id, "class_id": cls.id, "page": 1,
        "selected_text": "личная пометка", "start_offset": 0, "end_offset": 14,
    }, headers=auth_headers(student)).json()

    resp = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "Объясни"}],
        "class_id": cls.id,
        "annotation_id": ann["id"],
    }, headers=auth_headers(other_student))
    assert resp.status_code == 404


def test_plain_class_chat_still_works_without_fragment(client, db_session):
    _, student, cls, _ = _setup(db_session)
    resp = client.post("/api/ai/chat", json={
        "messages": [{"role": "user", "content": "Привет"}],
        "class_id": cls.id,
    }, headers=auth_headers(student))
    assert resp.status_code == 200
    assert "Пользователь спрашивает про конкретный фрагмент" not in "\n".join(_system_texts())
