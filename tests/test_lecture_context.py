"""Контекст материалов класса для ИИ: crud.posts.get_lecture_context (грейдинг
и деадлайн-автопроверка) и routers.ai._truncate_lecture_context (репетитор
класса). Регресс на баг: раньше get_lecture_context искал в body постов поля
{"type": "lecture", "class_id": ...}, которые не пишет ни один клиент (лекции
маркируются префиксом заголовка "[LECTURE][{class_id}]"), поэтому
lecture_context для проверки ИИ всегда получался пустым."""
import json as _json

import pytest

from crud import classes as crud_classes
from crud import assignments as crud_assignments
from crud import posts as crud_posts
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers
from tests.test_sync import _FakeClient


def _make_lecture(db, class_id, topic, content, user_id, files=None):
    body = _json.dumps({"content": content, **({"files": files} if files else {})})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] {topic}", body=body, user_id=user_id,
    )


# ── crud.posts.get_lecture_context ──────────────────────────────────────────

def test_get_lecture_context_scoped_to_class(db_session):
    teacher = make_user(db_session, role="teacher")
    class_a = crud_classes.create_class(db_session, "Math", None, created_by=teacher.id)
    class_b = crud_classes.create_class(db_session, "History", None, created_by=teacher.id)

    _make_lecture(db_session, class_a.id, "Векторы", "Вектор — направленный отрезок.", teacher.id)
    _make_lecture(db_session, class_b.id, "Renaissance", "The Renaissance began in Italy.", teacher.id)

    ctx = crud_posts.get_lecture_context(db_session, class_a.id)
    assert "Векторы" in ctx
    assert "Вектор — направленный отрезок." in ctx
    # BUG: лекции другого класса не должны утекать в контекст этого класса.
    assert "Renaissance" not in ctx


def test_get_lecture_context_orders_and_strips_title_prefix(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Physics", None, created_by=teacher.id)
    _make_lecture(db_session, cls.id, "Кинематика", "Первая лекция курса.", teacher.id)
    _make_lecture(db_session, cls.id, "Динамика", "Вторая лекция курса.", teacher.id)

    ctx = crud_posts.get_lecture_context(db_session, cls.id)
    # Заголовок отдаётся без служебного префикса "[LECTURE][id]".
    assert "[LECTURE]" not in ctx
    assert "### Кинематика" in ctx
    assert "### Динамика" in ctx
    # Хронологический порядок (по position): первая лекция раньше второй.
    assert ctx.index("Кинематика") < ctx.index("Динамика")


def test_get_lecture_context_empty_when_no_lectures(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Empty", None, created_by=teacher.id)
    assert crud_posts.get_lecture_context(db_session, cls.id) == ""


def test_get_lecture_context_never_used_broken_type_field(db_session):
    """Регресс: старая реализация ждала body == {"type": "lecture", "class_id":
    N, ...} — такого объекта никто не создаёт, поэтому раньше вернулся бы "".
    Пост с реальным форматом ({"content": ...}, class_id в title) должен
    попасть в контекст."""
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Chem", None, created_by=teacher.id)
    _make_lecture(db_session, cls.id, "Кислоты", "pH меньше 7 — кислота.", teacher.id)

    ctx = crud_posts.get_lecture_context(db_session, cls.id)
    assert ctx != ""
    assert "pH меньше 7" in ctx


# ── AI-grade: lecture_context реально доходит до промпта ────────────────────

class _CapturingClient:
    """Как _FakeClient, но запоминает JSON последнего запроса — чтобы
    проверить, что lecture_context попал в промпт, а не потерялся."""
    last_payload = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CapturingClient.last_payload = json

        class _R:
            is_success = True
            status_code = 200

            def json(self_inner):
                return {
                    "choices": [{"message": {"content": _json.dumps({
                        "confidence": 90,
                        "confidence_reasons": [],
                        "score": 80,
                        "feedback": "ok",
                        "criteria_scores": [{"name": "полнота", "score": 80, "max": 100, "comment": "ok"}],
                    })}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
        return _R()


def test_ai_grade_includes_lecture_context_in_prompt(client, db_session, monkeypatch):
    from services import ai_grader as ai_grader_module

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ai_grader_module, "httpx", ai_grader_module.httpx)
    monkeypatch.setattr(ai_grader_module.httpx, "AsyncClient", _CapturingClient)

    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "Bio", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls.id, student.id)
    _make_lecture(db_session, cls.id, "Клетка", "Клетка — структурная единица жизни.", teacher.id)

    assignment = crud_assignments.create_assignment(
        db=db_session, class_id=cls.id, title="HW", description=None,
        criteria=[{"name": "полнота", "weight": 100}], max_score=100,
        deadline=None, created_by=teacher.id,
    )
    # см. test_cross_class_access.py — после flush() нужен явный commit.
    db_session.commit()
    sub = client.post(
        f"/api/assignments/{assignment.id}/submit",
        json={"text_content": "Клетки состоят из мембраны и ядра."},
        headers=auth_headers(student),
    ).json()

    resp = client.post(
        f"/api/submissions/{sub['id']}/ai-grade",
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 200, resp.text

    payload = _CapturingClient.last_payload
    assert payload is not None
    user_msg = next(m["content"] for m in payload["messages"] if m["role"] == "user")
    assert "МАТЕРИАЛЫ КУРСА" in user_msg
    assert "Клетка — структурная единица жизни." in user_msg


# ── /ai/chat: обрезка материалов класса по границе лекции ───────────────────

def test_truncate_lecture_context_keeps_short_text_intact():
    text = "### Лекция 1: Тема\nКороткий текст."
    assert ai_module._truncate_lecture_context(text, limit=10_000) == text


def test_truncate_lecture_context_cuts_on_lecture_boundary_not_midword():
    lecture1 = "### Лекция 1: Тема A\n" + ("а" * 100)
    lecture2 = "### Лекция 2: Тема B\n" + ("б" * 100)
    text = lecture1 + "\n\n" + lecture2
    # Лимит режет ровно посреди второй лекции.
    limit = len(lecture1) + 10
    result = ai_module._truncate_lecture_context(text, limit=limit)
    # Лекция 1 сохранена целиком.
    assert lecture1 in result
    # Лекция 2 не обрезана посередине — либо целиком отсутствует, либо
    # заголовок "### Лекция 2" не разорван на полуслове.
    assert "### Лекция 2: Тема B" not in result or lecture2 in result
    assert not result.startswith(lecture1 + "\n\n### Лекция 2: Тема B\nбб")
    # Модель предупреждена, что часть материалов не поместилась.
    assert "не поместилась" in result


def test_truncate_lecture_context_never_exceeds_limit_plus_note():
    text = "\n\n".join(f"### Лекция {i}\n" + ("x" * 3000) for i in range(1, 30))
    result = ai_module._truncate_lecture_context(text, limit=8_000)
    assert len(result) < len(text)
    assert "не поместилась" in result
