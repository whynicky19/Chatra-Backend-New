"""Регрессия: раньше AssignmentUpdate.deadline=None пропадал в
body.model_dump(exclude_none=True) и не отличался от "поле не прислали" —
снять дедлайн через PUT /assignments/{id} было невозможно (тихий no-op).
clear_deadline — явный флаг для этого случая (см. схему AssignmentUpdate)."""
from datetime import datetime, timedelta, timezone

from crud import classes as crud_classes
from crud import assignments as crud_assignments
from crud import cohorts as crud_cohorts
from tests.conftest import make_user, auth_headers


def test_clear_deadline_flag_removes_deadline(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Chem", None, created_by=teacher.id)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)
    assignment = crud_assignments.create_assignment(
        db_session, cls.id, "HW1", None, [{"name": "ok", "weight": 100}], 100,
        deadline, teacher.id,
    )
    cohort = crud_cohorts.get_active_cohort(db_session, cls.id)
    crud_cohorts.upsert_deadline(db_session, cohort.id, assignment.id, deadline, is_published=True)
    db_session.commit()

    resp = client.put(
        f"/api/assignments/{assignment.id}",
        json={"clear_deadline": True},
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 200
    assert resp.json()["deadline"] is None

    # Deadline-строка потока тоже должна пропасть (due_date NOT NULL — значит
    # "без дедлайна" это отсутствие строки, не due_date=None).
    assert crud_cohorts.get_active_cohort(db_session, cls.id) is not None
    row = None
    from models import Deadline
    row = (
        db_session.query(Deadline)
        .filter(Deadline.cohort_id == cohort.id, Deadline.assignment_id == assignment.id)
        .first()
    )
    assert row is None


def test_omitting_deadline_field_does_not_clear_it(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Chem2", None, created_by=teacher.id)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)
    assignment = crud_assignments.create_assignment(
        db_session, cls.id, "HW1", None, [{"name": "ok", "weight": 100}], 100,
        deadline, teacher.id,
    )

    resp = client.put(
        f"/api/assignments/{assignment.id}",
        json={"title": "HW1 renamed"},
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 200
    assert resp.json()["deadline"] is not None
    assert resp.json()["title"] == "HW1 renamed"
