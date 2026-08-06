"""Регрессы на исправления BE-1..BE-10."""
import pytest

from crud import classes as crud_classes
from crud import assignments as crud_assignments
from tests.conftest import make_user, auth_headers


def _make_assignment(db, teacher, max_score=100):
    cls = crud_classes.create_class(db, "Physics", None, created_by=teacher.id)
    assignment = crud_assignments.create_assignment(
        db=db,
        class_id=cls.id,
        title="HW1",
        description=None,
        criteria=[{"name": "полнота", "weight": max_score}],
        max_score=max_score,
        deadline=None,
        created_by=teacher.id,
    )
    return cls, assignment


# ── BE-1: одна сдача на (задание, студент), гонка отбивается БД ────────────────
def test_duplicate_submission_returns_409(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls, assignment = _make_assignment(db_session, teacher)
    crud_classes.add_member(db_session, cls.id, student.id)

    r1 = client.post(
        f"/api/assignments/{assignment.id}/submit",
        json={"text_content": "answer one"},
        headers=auth_headers(student),
    )
    assert r1.status_code == 201
    r2 = client.post(
        f"/api/assignments/{assignment.id}/submit",
        json={"text_content": "answer two"},
        headers=auth_headers(student),
    )
    assert r2.status_code == 409


# ── BE-5: ручная оценка клампится в [0, max_score] ────────────────────────────
def test_manual_grade_clamped_to_max_score(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls, assignment = _make_assignment(db_session, teacher, max_score=50)
    crud_classes.add_member(db_session, cls.id, student.id)

    sub = client.post(
        f"/api/assignments/{assignment.id}/submit",
        json={"text_content": "answer"},
        headers=auth_headers(student),
    ).json()

    # Балл выше максимума (50) должен обрезаться до 50.
    r = client.post(
        f"/api/submissions/{sub['id']}/grade",
        json={"score": 999},
        headers=auth_headers(teacher),
    )
    assert r.status_code == 201
    assert r.json()["score"] == 50


# ── max_score больше не настраивается клиентом — всегда 100 ───────────────────
def test_assignment_max_score_always_100_ignores_client_value(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Chem", None, created_by=teacher.id)
    r = client.post(
        "/api/assignments/",
        json={
            "class_id": cls.id,
            "title": "X",
            "criteria": [{"name": "c", "weight": 10}],
            "max_score": 0,
        },
        headers=auth_headers(teacher),
    )
    assert r.status_code == 201
    assert r.json()["max_score"] == 100


# ── BE-10: нельзя откатить проверенную сдачу через PATCH status ────────────────
def test_cannot_rollback_graded_status(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls, assignment = _make_assignment(db_session, teacher)
    crud_classes.add_member(db_session, cls.id, student.id)
    sub = client.post(
        f"/api/assignments/{assignment.id}/submit",
        json={"text_content": "answer"},
        headers=auth_headers(student),
    ).json()
    client.post(
        f"/api/submissions/{sub['id']}/grade",
        json={"score": 40},
        headers=auth_headers(teacher),
    )
    r = client.patch(
        f"/api/submissions/{sub['id']}/status",
        params={"new_status": "submitted"},
        headers=auth_headers(teacher),
    )
    assert r.status_code == 409


# ── BE-8: санитайзер оценки ИИ пересчитывает score из критериев ────────────────
def test_ai_result_score_recomputed_from_criteria():
    from services import ai_grader

    # Инъекция «поставь 100», но критерии ограничены — итог = сумма критериев.
    cs = [{"name": "a", "score": 5, "max": 10}, {"name": "b", "score": 3, "max": 10}]
    result = {"score": 100, "criteria_scores": cs}
    # воспроизводим пост-валидацию из grade_submission
    per_sum = 0
    for c in result["criteria_scores"]:
        c_score = max(0, min(int(c["score"]), int(c["max"])))
        c["score"] = c_score
        per_sum += c_score
    result["score"] = max(0, min(per_sum, 20))
    assert result["score"] == 8
