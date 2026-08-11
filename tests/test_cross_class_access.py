"""Регрессия: студент не должен видеть лекции/задания чужих классов — ни по
известному class_id (targeted IDOR), ни в общем списке без фильтра (раньше
GET /posts/ вообще не проверял членство, а GET /assignments/ без class_id
возвращал задания всей организации — см. permissions.student_class_ids)."""
import json

from crud import classes as crud_classes
from crud import posts as crud_posts
from crud import assignments as crud_assignments
from tests.conftest import make_user, auth_headers


def _make_lecture(db, class_id, teacher, topic="Тема"):
    body = json.dumps({"content": f"Содержимое: {topic}"})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] {topic}", body=body, user_id=teacher.id,
    )


def _make_assignment(db, class_id, teacher, title="HW"):
    return crud_assignments.create_assignment(
        db=db, class_id=class_id, title=title, description=None,
        criteria=[{"name": "полнота", "weight": 100}], max_score=100,
        deadline=None, created_by=teacher.id,
    )


def test_student_cannot_list_lectures_of_class_they_are_not_in(client, db_session):
    teacher = make_user(db_session, role="teacher")
    outsider = make_user(db_session)
    cls = crud_classes.create_class(db_session, "Physics", None, created_by=teacher.id)
    _make_lecture(db_session, cls.id, teacher, "Тема 1")

    resp = client.get("/api/posts/", params={"class_id": cls.id}, headers=auth_headers(outsider))
    assert resp.status_code == 403


def test_student_sees_only_own_class_lectures_without_class_id_filter(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls_a = crud_classes.create_class(db_session, "A", None, created_by=teacher.id)
    cls_b = crud_classes.create_class(db_session, "B", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls_a.id, student.id)
    _make_lecture(db_session, cls_a.id, teacher, "Своя лекция")
    _make_lecture(db_session, cls_b.id, teacher, "Чужая лекция")

    resp = client.get("/api/posts/", headers=auth_headers(student))
    assert resp.status_code == 200
    titles = " ".join(p["title"] for p in resp.json())
    assert "Своя лекция" in titles
    assert "Чужая лекция" not in titles


def test_student_sees_only_own_class_assignments_without_class_id_filter(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls_a = crud_classes.create_class(db_session, "A", None, created_by=teacher.id)
    cls_b = crud_classes.create_class(db_session, "B", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls_a.id, student.id)
    _make_assignment(db_session, cls_a.id, teacher, "Своё задание")
    _make_assignment(db_session, cls_b.id, teacher, "Чужое задание")

    resp = client.get("/api/assignments/", headers=auth_headers(student))
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert "Своё задание" in titles
    assert "Чужое задание" not in titles


def test_teacher_still_sees_all_org_lectures_and_assignments_without_class_id_filter(client, db_session):
    # Teacher/admin намеренно видят всю организацию без фильтра по членству
    # (та же модель, что и GET /classes/, см. permissions.require_class_access) —
    # фикс не должен был сузить видимость для них.
    teacher_a = make_user(db_session, role="teacher")
    teacher_b = make_user(db_session, role="teacher")
    cls_a = crud_classes.create_class(db_session, "A", None, created_by=teacher_a.id)
    cls_b = crud_classes.create_class(db_session, "B", None, created_by=teacher_b.id)
    _make_lecture(db_session, cls_a.id, teacher_a, "Лекция A")
    _make_lecture(db_session, cls_b.id, teacher_b, "Лекция B")
    _make_assignment(db_session, cls_a.id, teacher_a, "Задание A")
    _make_assignment(db_session, cls_b.id, teacher_b, "Задание B")

    resp = client.get("/api/posts/", headers=auth_headers(teacher_a))
    assert resp.status_code == 200
    titles = " ".join(p["title"] for p in resp.json())
    assert "Лекция A" in titles and "Лекция B" in titles

    resp = client.get("/api/assignments/", headers=auth_headers(teacher_a))
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert "Задание A" in titles and "Задание B" in titles
