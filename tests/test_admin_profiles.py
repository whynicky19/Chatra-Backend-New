"""Карточки пользователя и предмета в админке.

Проверяем то, ради чего ручки появились: админ должен видеть по человеку и по
предмету всё сразу — расход ИИ с разбивкой, классы, учебную активность и
последнее действие, без десятка догрузок из браузера.
"""
from datetime import timedelta

import pytest

from models import (Assignment, AiUsageLog, Class, Cohort, Grade, Posts, Submission,
                    class_members, cohort_students)
from tests.conftest import auth_headers, make_user
from utils.time import utcnow

_counter = {"n": 0}


@pytest.fixture()
def clean_usage(db_session):
    db_session.query(AiUsageLog).delete()
    db_session.commit()
    return db_session


@pytest.fixture()
def admin(db_session):
    return make_user(db_session, role="admin")


def _class(db, teacher, name="Алгебра"):
    _counter["n"] += 1
    cls = Class(name=name, created_by=teacher.id, org_type=teacher.org_type,
                invite_code=f"PRF{_counter['n']:05d}")
    db.add(cls)
    db.commit()
    db.refresh(cls)
    return cls


def _join(db, cls, user):
    db.execute(class_members.insert().values(class_id=cls.id, user_id=user.id))
    db.commit()


def _log(db, *, user, endpoint="chat", total=100, class_id=None, days_ago=0):
    db.add(AiUsageLog(user_id=user.id, class_id=class_id, endpoint=endpoint,
                      org_type=user.org_type, prompt_tokens=total // 2,
                      completion_tokens=total - total // 2, total_tokens=total,
                      created_at=utcnow() - timedelta(days=days_ago)))
    db.commit()


def _assignment(db, cls, teacher, title="Работа 1"):
    a = Assignment(class_id=cls.id, title=title, criteria="{}", created_by=teacher.id,
                   max_score=100)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _submission(db, assignment, student, score=None):
    s = Submission(assignment_id=assignment.id, student_id=student.id, text_content="ответ")
    db.add(s)
    db.commit()
    db.refresh(s)
    if score is not None:
        db.add(Grade(submission_id=s.id, score=score))
        db.commit()
    return s


# ── Карточка пользователя ────────────────────────────────────────────────────

def test_user_card_has_ai_classes_and_activity(client, db_session, admin, clean_usage):
    """Всё, что показывает карточка, приходит одним запросом."""
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    student.full_name = "Небогин Никита"
    db_session.commit()
    cls = _class(db_session, teacher, name="Mathematics")
    _join(db_session, cls, student)

    _log(db_session, user=student, endpoint="chat", total=5000, class_id=cls.id)
    _log(db_session, user=student, endpoint="ai_title", total=120, class_id=cls.id)
    _log(db_session, user=student, endpoint="chat", total=800)   # общий ассистент
    assignment = _assignment(db_session, cls, teacher)
    _submission(db_session, assignment, student, score=80)
    db_session.add(Posts(title="Заметка", body="{}", user_id=student.id))
    db_session.commit()

    data = client.get(f"/api/admin/users/{student.id}", headers=auth_headers(admin)).json()

    assert data["full_name"] == "Небогин Никита"
    assert data["role"] == "student"
    assert data["is_active"] is True
    assert data["ai"]["total_tokens"] == 5920
    assert data["ai"]["request_count"] == 3
    kinds = {k["endpoint"]: k["total_tokens"] for k in data["ai"]["by_endpoint"]}
    assert kinds == {"chat": 5800, "ai_title": 120}
    # Расход вне предметов не должен потеряться между классами.
    assert data["ai"]["general_tokens"] == 800
    assert [c["name"] for c in data["classes"]] == ["Mathematics"]
    assert data["classes"][0]["total_tokens"] == 5120
    assert data["classes"][0]["role"] == "member"
    assert data["activity"]["submissions"] == 1
    assert data["activity"]["posts"] == 1
    assert data["activity"]["avg_score"] == 80.0
    assert data["last_active"]


def test_user_card_shows_daily_message_quota(client, db_session, admin, clean_usage, monkeypatch):
    """В карточке видно, сколько сообщений человек израсходовал из дневного
    лимита — это то, по чему бэкенд реально отказывает в запросе."""
    monkeypatch.setenv("AI_DAILY_MESSAGE_LIMIT", "50")
    student = make_user(db_session, role="student")
    _log(db_session, user=student, endpoint="chat", total=100)
    _log(db_session, user=student, endpoint="chat_vision", total=100)
    # Вчерашние сообщения в сегодняшнюю квоту не входят.
    _log(db_session, user=student, endpoint="chat", total=100, days_ago=2)

    ai = client.get(f"/api/admin/users/{student.id}", headers=auth_headers(admin)).json()["ai"]

    assert ai["messages_today"] == 2
    assert ai["message_limit"] == 50


def test_user_card_marks_created_classes(client, db_session, admin, clean_usage):
    """Преподаватель показывается как создатель своего предмета, даже если его
    нет в списке участников."""
    teacher = make_user(db_session, role="teacher")
    cls = _class(db_session, teacher, name="Physics")
    _assignment(db_session, cls, teacher)

    data = client.get(f"/api/admin/users/{teacher.id}", headers=auth_headers(admin)).json()

    assert [(c["name"], c["role"]) for c in data["classes"]] == [("Physics", "creator")]
    assert data["activity"]["assignments_created"] == 1


def test_user_card_registration_date_may_be_unknown(client, db_session, admin):
    """У аккаунтов до migrations/022 даты регистрации нет — отдаём null, а не
    выдуманную дату."""
    student = make_user(db_session, role="student")
    student.created_at = None
    db_session.commit()

    data = client.get(f"/api/admin/users/{student.id}", headers=auth_headers(admin)).json()
    assert data["created_at"] is None

    fresh = make_user(db_session, role="student")
    fresh_data = client.get(f"/api/admin/users/{fresh.id}", headers=auth_headers(admin)).json()
    assert fresh_data["created_at"], "новая регистрация обязана получить дату"


def test_user_card_is_scoped_and_admin_only(client, db_session, admin):
    stranger = make_user(db_session, role="student", org_type="school")
    assert client.get(f"/api/admin/users/{stranger.id}",
                      headers=auth_headers(admin)).status_code == 404
    assert client.get(f"/api/admin/users/{admin.id}",
                      headers=auth_headers(make_user(db_session, role="teacher"))).status_code == 403
    assert client.get("/api/admin/users/999999",
                      headers=auth_headers(admin)).status_code == 404


# ── Список пользователей с агрегатами ────────────────────────────────────────

def test_users_overview_carries_aggregates(client, db_session, admin, clean_usage):
    """Список показывает расход и классы сразу — иначе на каждую строку
    пришлось бы делать отдельный запрос."""
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = _class(db_session, teacher, name="English")
    _join(db_session, cls, student)
    _log(db_session, user=student, endpoint="chat", total=700, class_id=cls.id)

    rows = {r["id"]: r for r in client.get("/api/admin/users/overview",
                                           headers=auth_headers(admin)).json()}

    assert rows[student.id]["total_tokens"] == 700
    assert rows[student.id]["request_count"] == 1
    assert rows[student.id]["class_count"] == 1
    assert rows[student.id]["last_active"]
    # Не тративший ИИ — нули, а не отсутствие строки.
    assert rows[admin.id]["total_tokens"] == 0
    assert rows[admin.id]["last_active"] is None


def test_users_overview_is_scoped_to_org(client, db_session, admin, clean_usage):
    stranger = make_user(db_session, role="student", org_type="school")
    ids = {r["id"] for r in client.get("/api/admin/users/overview",
                                       headers=auth_headers(admin)).json()}
    assert admin.id in ids
    assert stranger.id not in ids


def test_users_overview_not_shadowed_by_user_id_route(client, db_session, admin):
    """/users/overview не должен разбираться как /users/{user_id}."""
    assert client.get("/api/admin/users/overview",
                      headers=auth_headers(admin)).status_code == 200


# ── Карточка предмета ────────────────────────────────────────────────────────

def test_class_card_has_members_content_and_ai(client, db_session, admin, clean_usage):
    teacher = make_user(db_session, role="teacher")
    teacher.full_name = "Иванова Мария"
    heavy = make_user(db_session, role="student")
    light = make_user(db_session, role="student")
    db_session.commit()
    cls = _class(db_session, teacher, name="Chemistry")
    _join(db_session, cls, heavy)
    _join(db_session, cls, light)

    _log(db_session, user=heavy, endpoint="chat", total=4000, class_id=cls.id)
    _log(db_session, user=heavy, endpoint="cover_image", total=300, class_id=cls.id)
    _log(db_session, user=light, endpoint="chat", total=100, class_id=cls.id)
    # Расход другого предмета в карточку попадать не должен.
    other = _class(db_session, teacher, name="Другой")
    _log(db_session, user=heavy, endpoint="chat", total=9999, class_id=other.id)

    assignment = _assignment(db_session, cls, teacher)
    _submission(db_session, assignment, heavy, score=90)
    _submission(db_session, assignment, light)
    db_session.add(Posts(title=f"[LECTURE][{cls.id}] Введение", body="{}", user_id=teacher.id))
    db_session.commit()

    data = client.get(f"/api/admin/classes/{cls.id}", headers=auth_headers(admin)).json()

    assert data["name"] == "Chemistry"
    assert data["creator"]["full_name"] == "Иванова Мария"
    assert data["ai"]["total_tokens"] == 4400
    assert {k["endpoint"] for k in data["ai"]["by_endpoint"]} == {"chat", "cover_image"}
    # Участники отсортированы по расходу — самый дорогой сверху.
    assert data["members"][0]["id"] == heavy.id
    assert data["members"][0]["total_tokens"] == 4300
    assert data["member_count"] == 2
    assert data["content"]["assignments"] == 1
    assert data["content"]["lectures"] == 1
    assert data["content"]["submissions"] == 2
    assert data["content"]["graded"] == 1
    assert data["content"]["avg_score"] == 90.0


def test_class_card_lists_cohorts(client, db_session, admin, clean_usage):
    """Потоки — часть карточки: по ним видно, сколько учеников в каком году."""
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = _class(db_session, teacher, name="History")
    cohort = Cohort(class_id=cls.id, academic_year="2026/2027",
                    start_date=utcnow().date(), status="active")
    db_session.add(cohort)
    db_session.commit()
    db_session.execute(cohort_students.insert().values(cohort_id=cohort.id,
                                                       student_id=student.id))
    db_session.commit()

    data = client.get(f"/api/admin/classes/{cls.id}", headers=auth_headers(admin)).json()

    assert data["cohorts"][0]["academic_year"] == "2026/2027"
    assert data["cohorts"][0]["status"] == "active"
    assert data["cohorts"][0]["student_count"] == 1


def test_class_card_is_scoped_and_admin_only(client, db_session, admin):
    teacher = make_user(db_session, role="teacher", org_type="school")
    foreign = _class(db_session, teacher, name="Чужой")
    assert client.get(f"/api/admin/classes/{foreign.id}",
                      headers=auth_headers(admin)).status_code == 404
    own_teacher = make_user(db_session, role="teacher")
    own = _class(db_session, own_teacher, name="Свой")
    assert client.get(f"/api/admin/classes/{own.id}",
                      headers=auth_headers(own_teacher)).status_code == 403
