"""Регрессионные тесты на исправления аудита безопасности:
регистрация ролей, IDOR (чаты/сообщения/реакции/задания), SSRF-хелпер."""
import json

import pytest
from sqlalchemy import text

from crud import classes as crud_classes
from crud import assignments as crud_assignments
from models import Chat, chat_members, Message
from services.url_safety import is_safe_fetch_url
from tests.conftest import make_user, auth_headers


# ── 1) Privilege escalation при регистрации ──────────────────────────────────

def test_register_ignores_role_from_body(client):
    resp = client.post("/auth/register", json={
        "email": "wannabe-admin@example.com",
        "password": "password123",
        "role": "admin",
        "full_name": "Hacker Person",
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "student"


def test_register_rejects_short_password(client):
    resp = client.post("/auth/register", json={
        "email": "shortpw@example.com",
        "password": "1234567",
    })
    assert resp.status_code == 422


def test_admin_create_user_rejects_unknown_role(client, db_session):
    admin = make_user(db_session, role="admin")
    resp = client.post("/admin/users", json={
        "email": "x@example.com",
        "password": "password123",
        "role": "superadmin",
    }, headers=auth_headers(admin))
    assert resp.status_code == 422


def test_admin_set_role_rejects_unknown_role(client, db_session):
    admin = make_user(db_session, role="admin")
    victim = make_user(db_session)
    resp = client.put(
        f"/admin/users/{victim.id}/role",
        params={"new_role": "root"},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 422


# ── 7) IDOR: чаты / сообщения / реакции ──────────────────────────────────────

def _make_chat_with_message(db, member):
    chat = Chat(name="private")
    db.add(chat)
    db.commit()
    db.execute(chat_members.insert().values(chat_id=chat.id, user_id=member.id))
    msg = Message(content="secret", chat_id=chat.id, user_id=member.id)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return chat, msg


def test_chat_users_requires_membership(client, db_session):
    member = make_user(db_session)
    outsider = make_user(db_session)
    chat, _ = _make_chat_with_message(db_session, member)

    resp = client.get(f"/chats/{chat.id}/users", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.get(f"/chats/{chat.id}/users", headers=auth_headers(member))
    assert resp.status_code == 200


def test_chat_add_remove_user_requires_membership(client, db_session):
    member = make_user(db_session)
    outsider = make_user(db_session)
    chat, _ = _make_chat_with_message(db_session, member)

    resp = client.post(f"/chats/{chat.id}/users/{outsider.id}", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.delete(f"/chats/{chat.id}/users/{member.id}", headers=auth_headers(outsider))
    assert resp.status_code == 403


def test_chat_endpoints_missing_chat_404(client, db_session):
    user = make_user(db_session)
    assert client.get("/chats/999999/users", headers=auth_headers(user)).status_code == 404


def test_mark_read_requires_membership(client, db_session):
    member = make_user(db_session)
    outsider = make_user(db_session)
    _, msg = _make_chat_with_message(db_session, member)

    resp = client.put(f"/messages/{msg.id}/read", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.put(f"/messages/{msg.id}/read", headers=auth_headers(member))
    assert resp.status_code == 200


def test_reactions_require_membership(client, db_session):
    member = make_user(db_session)
    outsider = make_user(db_session)
    _, msg = _make_chat_with_message(db_session, member)

    resp = client.post(f"/reactions/{msg.id}", params={"emoji": "👍"}, headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.delete(f"/reactions/{msg.id}", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.post(f"/reactions/{msg.id}", params={"emoji": "👍"}, headers=auth_headers(member))
    assert resp.status_code == 200


# ── 7) IDOR: задания доступны только участникам класса ───────────────────────

def _make_assignment(db, teacher):
    cls = crud_classes.create_class(db, "Physics", None, created_by=teacher.id)
    assignment = crud_assignments.create_assignment(
        db=db,
        class_id=cls.id,
        title="HW1",
        description=None,
        criteria=[{"name": "полнота", "weight": 100}],
        max_score=100,
        deadline=None,
        created_by=teacher.id,
    )
    return cls, assignment


def test_student_outside_class_cannot_read_assignment(client, db_session):
    teacher = make_user(db_session, role="teacher")
    outsider = make_user(db_session)
    cls, assignment = _make_assignment(db_session, teacher)

    resp = client.get(f"/assignments/{assignment.id}", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.get(f"/assignments/{assignment.id}/variants", headers=auth_headers(outsider))
    assert resp.status_code == 403
    resp = client.post(
        f"/assignments/{assignment.id}/submit",
        json={"text_content": "my answer"},
        headers=auth_headers(outsider),
    )
    assert resp.status_code == 403


def test_class_member_can_read_and_submit_assignment(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls, assignment = _make_assignment(db_session, teacher)
    crud_classes.add_member(db_session, cls.id, student.id)

    resp = client.get(f"/assignments/{assignment.id}", headers=auth_headers(student))
    assert resp.status_code == 200
    resp = client.post(
        f"/assignments/{assignment.id}/submit",
        json={"text_content": "my answer"},
        headers=auth_headers(student),
    )
    assert resp.status_code == 201


# ── 3) SSRF ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9200/_cat/indices",
    "http://10.0.0.5/admin",
    "http://172.16.3.4/latest/meta-data/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]:8000/uploads/x.txt",
    "https://evil.example.com/payload.docx",
    "file:///etc/passwd",
    "ftp://internal/file",
    "",
])
def test_unsafe_urls_rejected(url):
    assert is_safe_fetch_url(url) is False


def test_app_base_url_allowed():
    # conftest не задаёт APP_BASE_URL — действует дефолт http://localhost:8000
    assert is_safe_fetch_url("http://localhost:8000/uploads/file.pdf") is True
    # но чужой порт/хост под тем же префиксом строки не проходит
    assert is_safe_fetch_url("http://localhost:8000.evil.com/uploads/f.pdf") is False
    assert is_safe_fetch_url("http://localhost:9999/uploads/file.pdf") is False


def test_file_text_endpoint_blocks_private_url(client, db_session):
    user = make_user(db_session)
    resp = client.get(
        "/upload/utils/file-text",
        params={"url": "http://169.254.169.254/latest/meta-data/"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 400
