"""Потоки (когорты) и переход учебного года: вступление в активный поток,
read-only архив, изоляция потоков, сдвиг дедлайнов, черновики, ограничение
«один активный поток», идемпотентность rollover."""
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from models import Cohort, Deadline, Submission, cohort_students, class_members
from tests.conftest import make_user, auth_headers


def _setup_class(client, db, teacher, deadline="2025-10-01T23:59:00"):
    """Класс + задание с дедлайном через API (как это делает Flutter)."""
    cls = client.post("/classes/", json={"name": "Math"}, headers=auth_headers(teacher)).json()
    assignment = client.post(
        "/assignments/",
        json={
            "class_id": cls["id"],
            "title": "HW1",
            "criteria": [{"name": "ok", "weight": 100}],
            "deadline": deadline,
        },
        headers=auth_headers(teacher),
    ).json()
    return cls, assignment


def _rollover(client, teacher, class_id, year="2026/2027", start="2026-09-01"):
    client.patch(
        f"/classes/{class_id}/rotation-mode",
        json={"rotation_mode": "yearly"},
        headers=auth_headers(teacher),
    )
    resp = client.post(
        "/rollover",
        json={"class_ids": [class_id], "new_academic_year": year, "new_start_date": start},
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()[0]


def _active_cohort(db, class_id) -> Cohort:
    return (
        db.query(Cohort)
        .filter(Cohort.class_id == class_id, Cohort.status == "active")
        .one()
    )


def test_join_by_code_lands_in_active_cohort(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls, _ = _setup_class(client, db_session, teacher)

    resp = client.post(
        "/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(student)
    )
    assert resp.status_code == 200

    cohort = _active_cohort(db_session, cls["id"])
    row = db_session.execute(
        cohort_students.select().where(
            cohort_students.c.cohort_id == cohort.id,
            cohort_students.c.student_id == student.id,
        )
    ).fetchone()
    assert row is not None
    # Двойная запись: class_members продолжает наполняться (легаси-читатели).
    legacy = db_session.execute(
        class_members.select().where(
            class_members.c.class_id == cls["id"],
            class_members.c.user_id == student.id,
        )
    ).fetchone()
    assert legacy is not None


def test_rollover_old_student_sees_archive_read_only(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls, assignment = _setup_class(client, db_session, teacher)
    client.post("/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(student))

    result = _rollover(client, teacher, cls["id"])
    assert result["status"] == "rolled"

    # Не активный, но виден как архив
    my = client.get("/classes/", headers=auth_headers(student)).json()
    assert [(c["id"], c["is_archived_for_user"]) for c in my] == [(cls["id"], True)]
    one = client.get(f"/classes/{cls['id']}", headers=auth_headers(student)).json()
    assert one["is_archived_for_user"] is True

    # Чтение задания работает, сдача — 403 с понятным сообщением
    resp = client.get(f"/assignments/{assignment['id']}", headers=auth_headers(student))
    assert resp.status_code == 200
    resp = client.post(
        f"/assignments/{assignment['id']}/submit",
        json={"text_content": "x"},
        headers=auth_headers(student),
    )
    assert resp.status_code == 403
    assert "архив" in resp.json()["detail"]


def test_new_student_joins_new_cohort_and_does_not_see_others(client, db_session):
    teacher = make_user(db_session, role="teacher")
    old_student = make_user(db_session, role="student")
    cls, assignment = _setup_class(client, db_session, teacher)
    client.post("/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(old_student))
    old_sub = client.post(
        f"/assignments/{assignment['id']}/submit",
        json={"text_content": "old work"},
        headers=auth_headers(old_student),
    ).json()

    result = _rollover(client, teacher, cls["id"])
    new_cohort_id = result["new_cohort_id"]

    new_student = make_user(db_session, role="student")
    resp = client.post(
        "/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(new_student)
    )
    assert resp.status_code == 200
    assert db_session.execute(
        cohort_students.select().where(
            cohort_students.c.cohort_id == new_cohort_id,
            cohort_students.c.student_id == new_student.id,
        )
    ).fetchone() is not None

    # Новый ученик не видит класс архивным и не видит чужих сдач
    one = client.get(f"/classes/{cls['id']}", headers=auth_headers(new_student)).json()
    assert one["is_archived_for_user"] is False
    mine = client.get("/assignments/student/my-submissions", headers=auth_headers(new_student)).json()
    assert mine == []
    resp = client.get(f"/submissions/{old_sub['id']}", headers=auth_headers(new_student))
    assert resp.status_code == 403

    # Преподаватель по умолчанию видит сдачи активного потока (пусто),
    # старые — только явно через cohort_id архивного потока.
    subs = client.get(
        f"/assignments/{assignment['id']}/submissions", headers=auth_headers(teacher)
    ).json()
    assert subs == []
    old_cohort = (
        db_session.query(Cohort)
        .filter(Cohort.class_id == cls["id"], Cohort.status == "archived")
        .one()
    )
    subs = client.get(
        f"/assignments/{assignment['id']}/submissions",
        params={"cohort_id": old_cohort.id},
        headers=auth_headers(teacher),
    ).json()
    assert [s["id"] for s in subs] == [old_sub["id"]]


def test_rollover_shifts_deadlines_by_start_date_diff(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls, assignment = _setup_class(client, db_session, teacher, deadline="2025-10-01T23:59:00")
    old_cohort = _active_cohort(db_session, cls["id"])
    old_row = (
        db_session.query(Deadline)
        .filter(Deadline.cohort_id == old_cohort.id, Deadline.assignment_id == assignment["id"])
        .one()
    )

    from datetime import date
    new_start = date(2026, 9, 1)
    result = _rollover(client, teacher, cls["id"], start=new_start.isoformat())
    assert result["deadlines_created"] == 1

    new_row = (
        db_session.query(Deadline)
        .filter(Deadline.cohort_id == result["new_cohort_id"])
        .one()
    )
    assert new_row.due_date == old_row.due_date + (new_start - old_cohort.start_date)
    assert new_row.is_published is False
    # Старый дедлайн не тронут
    db_session.refresh(old_row)
    assert old_row.due_date == datetime(2025, 10, 1, 23, 59)


def test_unpublished_deadlines_hidden_from_students(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls, assignment = _setup_class(client, db_session, teacher)
    result = _rollover(client, teacher, cls["id"])
    new_cohort_id = result["new_cohort_id"]

    student = make_user(db_session, role="student")
    client.post("/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(student))

    # Черновик: задание скрыто от студента целиком (404 и нет в списке);
    # преподаватель видит его с датой.
    resp = client.get(f"/assignments/{assignment['id']}", headers=auth_headers(student))
    assert resp.status_code == 404
    items = client.get(
        "/assignments/", params={"class_id": cls["id"]}, headers=auth_headers(student)
    ).json()
    assert items == []
    resp = client.get(f"/assignments/{assignment['id']}", headers=auth_headers(teacher)).json()
    assert resp["deadline"] is not None

    # По скрытому черновику нельзя сдать работу (задание для студента не существует)
    resp = client.post(
        f"/assignments/{assignment['id']}/submit",
        json={"text_content": "early"},
        headers=auth_headers(student),
    )
    assert resp.status_code == 404

    # Публикация — задание и дедлайн появились у студента
    resp = client.patch(
        f"/cohorts/{new_cohort_id}/deadlines/publish-all", headers=auth_headers(teacher)
    )
    assert resp.json() == {"published": 1}
    resp = client.get(f"/assignments/{assignment['id']}", headers=auth_headers(student))
    assert resp.status_code == 200
    items = client.get(
        "/assignments/", params={"class_id": cls["id"]}, headers=auth_headers(student)
    ).json()
    assert len(items) == 1 and items[0]["deadline"] is not None


def test_class_cannot_have_two_active_cohorts(db_session):
    teacher = make_user(db_session, role="teacher")
    from crud import classes as crud_classes
    cls = crud_classes.create_class(db_session, "Solo", None, created_by=teacher.id)

    from datetime import date
    db_session.add(Cohort(class_id=cls.id, academic_year="2099/2100",
                          start_date=date(2099, 9, 1), status="active"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_repeat_rollover_does_not_break_data(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls, assignment = _setup_class(client, db_session, teacher)
    client.post("/classes/join-by-code", json={"code": cls["invite_code"]}, headers=auth_headers(student))

    first = _rollover(client, teacher, cls["id"])
    assert first["status"] == "rolled"
    cohorts_before = db_session.query(Cohort).filter(Cohort.class_id == cls["id"]).count()
    deadlines_before = db_session.query(Deadline).count()

    second = _rollover(client, teacher, cls["id"])
    assert second["status"] == "already_rolled"
    assert second["new_cohort_id"] == first["new_cohort_id"]
    assert db_session.query(Cohort).filter(Cohort.class_id == cls["id"]).count() == cohorts_before
    assert db_session.query(Deadline).count() == deadlines_before


def test_rollover_and_cohort_views_owner_only(client, db_session):
    teacher = make_user(db_session, role="teacher")
    stranger = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls, _ = _setup_class(client, db_session, teacher)

    resp = client.post(
        "/rollover",
        json={"class_ids": [cls["id"]], "new_academic_year": "2026/2027",
              "new_start_date": "2026-09-01"},
        headers=auth_headers(stranger),
    )
    assert resp.status_code == 403
    assert client.get(f"/classes/{cls['id']}/cohorts", headers=auth_headers(stranger)).status_code == 403
    # Студенту учительские ручки закрыты ролью
    assert client.get(f"/classes/{cls['id']}/cohorts", headers=auth_headers(student)).status_code == 403
