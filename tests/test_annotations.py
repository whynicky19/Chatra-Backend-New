"""Выделения и заметки к лекциям: CRUD, приватность и контекст для ИИ.

Ключевое здесь — выделения личные (их не видит даже преподаватель класса) и
привязаны к лекции по серверным данным, а не по тому, что прислал клиент.
"""
import json

from crud import classes as crud_classes
from crud import posts as crud_posts
from tests.conftest import make_user, auth_headers


def _make_lecture(db, class_id, teacher, topic="Инкапсуляция"):
    return crud_posts.create_new_post(
        db,
        title=f"[LECTURE][{class_id}] {topic}",
        body=json.dumps({"content": f"Текст лекции про {topic}"}),
        user_id=teacher.id,
    )


def _payload(lecture_id, class_id, **over):
    body = {
        "lecture_id": lecture_id,
        "class_id": class_id,
        "file_index": 0,
        "page": 12,
        "selected_text": "инкапсуляция скрывает внутреннее состояние",
        "prefix": "Здесь ",
        "suffix": " от вызывающего кода",
        "start_offset": 120,
        "end_offset": 163,
        "color": "yellow",
    }
    body.update(over)
    return body


def _setup(db):
    teacher = make_user(db, role="teacher")
    student = make_user(db)
    cls = crud_classes.create_class(db, "OOP", None, created_by=teacher.id)
    crud_classes.add_member(db, cls.id, student.id)
    lecture = _make_lecture(db, cls.id, teacher)
    return teacher, student, cls, lecture


def test_create_list_update_delete(client, db_session):
    _, student, cls, lecture = _setup(db_session)
    h = auth_headers(student)

    created = client.post("/api/annotations", json=_payload(lecture.id, cls.id), headers=h)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["page"] == 12
    assert row["color"] == "yellow"
    assert row["start_offset"] == 120 and row["end_offset"] == 163
    assert row["prefix"] == "Здесь " and row["suffix"] == " от вызывающего кода"
    # Название лекции приходит сразу — список «Мои выделения» не должен
    # запрашивать каждую лекцию отдельно.
    assert row["lecture_title"] == "Инкапсуляция"

    listed = client.get("/api/annotations", params={"lecture_id": lecture.id}, headers=h)
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [row["id"]]

    patched = client.patch(
        f"/api/annotations/{row['id']}",
        json={"color": "green", "comment": "спросить на семинаре"},
        headers=h,
    )
    assert patched.status_code == 200
    assert patched.json()["color"] == "green"
    assert patched.json()["comment"] == "спросить на семинаре"
    assert patched.json()["updated_at"] >= row["updated_at"]

    assert client.delete(f"/api/annotations/{row['id']}", headers=h).status_code == 204
    assert client.get("/api/annotations", headers=h).json() == []


def test_annotations_are_private_even_from_class_teacher(client, db_session):
    teacher, student, cls, lecture = _setup(db_session)
    created = client.post(
        "/api/annotations", json=_payload(lecture.id, cls.id), headers=auth_headers(student)
    )
    ann_id = created.json()["id"]

    # Преподаватель-владелец класса не видит чужие пометки в своей же лекции.
    assert client.get("/api/annotations", headers=auth_headers(teacher)).json() == []
    assert client.patch(
        f"/api/annotations/{ann_id}", json={"color": "red"}, headers=auth_headers(teacher)
    ).status_code == 404
    assert client.delete(
        f"/api/annotations/{ann_id}", headers=auth_headers(teacher)
    ).status_code == 404


def test_cannot_annotate_lecture_of_foreign_class(client, db_session):
    teacher = make_user(db_session, role="teacher")
    outsider = make_user(db_session)
    cls = crud_classes.create_class(db_session, "Physics", None, created_by=teacher.id)
    lecture = _make_lecture(db_session, cls.id, teacher)

    resp = client.post(
        "/api/annotations", json=_payload(lecture.id, cls.id), headers=auth_headers(outsider)
    )
    assert resp.status_code == 403


def test_class_id_is_taken_from_lecture_not_from_client(client, db_session):
    """Иначе пометку можно было бы приписать чужому предмету и утащить её
    фрагмент в его ИИ-чат."""
    _, student, cls, lecture = _setup(db_session)
    resp = client.post(
        "/api/annotations",
        json=_payload(lecture.id, class_id=999999),
        headers=auth_headers(student),
    )
    assert resp.status_code == 201
    assert resp.json()["class_id"] == cls.id


def test_validation(client, db_session):
    _, student, cls, lecture = _setup(db_session)
    h = auth_headers(student)

    assert client.post(
        "/api/annotations", json=_payload(lecture.id, cls.id, color="purple"), headers=h
    ).status_code == 422
    assert client.post(
        "/api/annotations", json=_payload(lecture.id, cls.id, selected_text=""), headers=h
    ).status_code == 422
    assert client.post(
        "/api/annotations",
        json=_payload(lecture.id, cls.id, start_offset=200, end_offset=100),
        headers=h,
    ).status_code == 422
    assert client.post(
        "/api/annotations", json=_payload(999999, cls.id), headers=h
    ).status_code == 404


def test_updated_after_returns_only_changed(client, db_session):
    """Инкрементальная синхронизация: клиент дотягивает только новое."""
    _, student, cls, lecture = _setup(db_session)
    h = auth_headers(student)
    first = client.post("/api/annotations", json=_payload(lecture.id, cls.id), headers=h).json()
    second = client.post(
        "/api/annotations",
        json=_payload(lecture.id, cls.id, selected_text="второй фрагмент", start_offset=300, end_offset=315),
        headers=h,
    ).json()

    fresh = client.get(
        "/api/annotations", params={"updated_after": first["updated_at"]}, headers=h
    )
    assert [r["id"] for r in fresh.json()] == [second["id"]]


def test_list_filters_by_class(client, db_session):
    teacher, student, cls, lecture = _setup(db_session)
    other = crud_classes.create_class(db_session, "Math", None, created_by=teacher.id)
    crud_classes.add_member(db_session, other.id, student.id)
    other_lecture = _make_lecture(db_session, other.id, teacher, "Матрицы")
    h = auth_headers(student)

    client.post("/api/annotations", json=_payload(lecture.id, cls.id), headers=h)
    client.post("/api/annotations", json=_payload(other_lecture.id, other.id), headers=h)

    rows = client.get("/api/annotations", params={"class_id": other.id}, headers=h).json()
    assert [r["lecture_id"] for r in rows] == [other_lecture.id]
    assert rows[0]["lecture_title"] == "Матрицы"
