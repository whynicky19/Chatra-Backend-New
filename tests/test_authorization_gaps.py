"""Регрессии на дыры доступа, найденные при полном аудите бэкенда.

До этих правок все проверки ниже проходили «успешно» (201/204) для того, кто
не имеет к классу никакого отношения:
  * POST /assignments/    — задание в ЧУЖОМ классе (в т.ч. в другой организации);
  * POST /posts/create    — «лекция» в ЛЮБОМ классе от имени студента-постороннего;
  * POST/DELETE /classes/{id}/members — правка состава чужого класса.
"""
import json

from tests.conftest import auth_headers, make_user


def _make_class(client, teacher, name="Класс"):
    r = client.post("/api/classes/", json={"name": name}, headers=auth_headers(teacher))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assignment_body(class_id):
    return {
        "class_id": class_id,
        "title": "Задание",
        "criteria": [{"name": "Полнота", "weight": 10}],
    }


# ── POST /assignments/ ────────────────────────────────────────────────────────

def test_owner_can_create_assignment(client, db_session):
    owner = make_user(db_session, role="teacher")
    cid = _make_class(client, owner)
    r = client.post("/api/assignments/", json=_assignment_body(cid), headers=auth_headers(owner))
    assert r.status_code == 201, r.text


def test_admin_can_create_assignment_in_any_class_of_org(client, db_session):
    owner = make_user(db_session, role="teacher")
    admin = make_user(db_session, role="admin")
    cid = _make_class(client, owner)
    r = client.post("/api/assignments/", json=_assignment_body(cid), headers=auth_headers(admin))
    assert r.status_code == 201, r.text


def test_foreign_teacher_cannot_create_assignment_in_someone_elses_class(client, db_session):
    owner = make_user(db_session, role="teacher")
    intruder = make_user(db_session, role="teacher")
    cid = _make_class(client, owner)
    r = client.post("/api/assignments/", json=_assignment_body(cid), headers=auth_headers(intruder))
    assert r.status_code == 403, r.text


def test_teacher_from_other_org_cannot_create_assignment(client, db_session):
    owner = make_user(db_session, role="teacher", org_type="university")
    intruder = make_user(db_session, role="teacher", org_type="school")
    cid = _make_class(client, owner)
    r = client.post("/api/assignments/", json=_assignment_body(cid), headers=auth_headers(intruder))
    # Чужая организация — класса «не существует».
    assert r.status_code == 404, r.text


# ── Лекции (посты с префиксом [LECTURE][class_id]) ────────────────────────────

def _lecture_body(class_id, title="Лекция"):
    return {
        "title": f"[LECTURE][{class_id}] {title}",
        "body": json.dumps({"content": "материал"}, ensure_ascii=False),
    }


def test_class_owner_can_publish_lecture(client, db_session):
    owner = make_user(db_session, role="teacher")
    cid = _make_class(client, owner)
    r = client.post("/api/posts/create", json=_lecture_body(cid), headers=auth_headers(owner))
    assert r.status_code == 201, r.text


def test_outsider_student_cannot_inject_lecture_into_class(client, db_session):
    owner = make_user(db_session, role="teacher")
    outsider = make_user(db_session, role="student")
    cid = _make_class(client, owner)
    r = client.post("/api/posts/create", json=_lecture_body(cid, "Поддельная"),
                    headers=auth_headers(outsider))
    assert r.status_code == 403, r.text
    # И её действительно нет в материалах класса.
    listing = client.get(f"/api/posts/?class_id={cid}", headers=auth_headers(owner))
    assert listing.json() == []


def test_enrolled_student_cannot_publish_lecture(client, db_session):
    owner = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cid = _make_class(client, owner)
    assert client.post(f"/api/classes/{cid}/members", json={"user_id": student.id},
                       headers=auth_headers(owner)).status_code == 201
    r = client.post("/api/posts/create", json=_lecture_body(cid), headers=auth_headers(student))
    assert r.status_code == 403, r.text


def test_foreign_teacher_cannot_publish_lecture(client, db_session):
    owner = make_user(db_session, role="teacher")
    intruder = make_user(db_session, role="teacher")
    cid = _make_class(client, owner)
    r = client.post("/api/posts/create", json=_lecture_body(cid), headers=auth_headers(intruder))
    assert r.status_code == 403, r.text


def test_plain_post_without_lecture_prefix_still_allowed(client, db_session):
    user = make_user(db_session, role="student")
    r = client.post("/api/posts/create",
                    json={"title": "Обычный пост", "body": json.dumps({"content": "x"})},
                    headers=auth_headers(user))
    assert r.status_code == 201, r.text


def test_post_cannot_be_relabelled_into_foreign_class_lecture(client, db_session):
    owner = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cid = _make_class(client, owner)
    created = client.post("/api/posts/create",
                          json={"title": "Обычный пост", "body": json.dumps({"content": "x"})},
                          headers=auth_headers(student))
    post_id = created.json()["id"]
    r = client.put(f"/api/posts/{post_id}", json=_lecture_body(cid, "Подмена"),
                   headers=auth_headers(student))
    assert r.status_code == 403, r.text


# ── Состав класса ─────────────────────────────────────────────────────────────

def test_foreign_teacher_cannot_change_roster(client, db_session):
    owner = make_user(db_session, role="teacher")
    intruder = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cid = _make_class(client, owner)

    r = client.post(f"/api/classes/{cid}/members", json={"user_id": student.id},
                    headers=auth_headers(intruder))
    assert r.status_code == 403, r.text

    assert client.post(f"/api/classes/{cid}/members", json={"user_id": student.id},
                       headers=auth_headers(owner)).status_code == 201
    r = client.delete(f"/api/classes/{cid}/members/{student.id}", headers=auth_headers(intruder))
    assert r.status_code == 403, r.text
    # Ученик остался в классе.
    members = client.get(f"/api/classes/{cid}/members", headers=auth_headers(owner)).json()
    assert student.id in [m["id"] for m in members]


def test_cannot_add_member_from_another_org(client, db_session):
    owner = make_user(db_session, role="teacher", org_type="university")
    foreign_student = make_user(db_session, role="student", org_type="school")
    cid = _make_class(client, owner)
    r = client.post(f"/api/classes/{cid}/members", json={"user_id": foreign_student.id},
                    headers=auth_headers(owner))
    assert r.status_code == 404, r.text
    members = client.get(f"/api/classes/{cid}/members", headers=auth_headers(owner)).json()
    assert foreign_student.id not in [m["id"] for m in members]


# ── Длины полей, которые Postgres проверяет, а SQLite нет ─────────────────────

def test_overlong_notif_key_rejected_with_422(client, db_session):
    user = make_user(db_session, role="student")
    r = client.post("/api/notifications/state", json={"notif_key": "x" * 300, "read": True},
                    headers=auth_headers(user))
    assert r.status_code == 422, r.text
    ok = client.post("/api/notifications/state", json={"notif_key": "grade:12", "read": True},
                     headers=auth_headers(user))
    assert ok.status_code == 200, ok.text


def test_overlong_push_platform_rejected(client, db_session):
    user = make_user(db_session, role="student")
    r = client.post("/api/push/register", json={"token": "t-1", "platform": "a" * 64},
                    headers=auth_headers(user))
    assert r.status_code == 422, r.text
    ok = client.post("/api/push/register", json={"token": "t-1", "platform": "android"},
                     headers=auth_headers(user))
    assert ok.status_code == 200, ok.text
