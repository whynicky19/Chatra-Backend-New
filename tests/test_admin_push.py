"""Пуши админам: жалоба и заявка на аватар должны уведомлять админов
своей организации (services.fcm.notify_admins → send_push_bg)."""
import services.fcm as fcm
from tests.conftest import make_user, auth_headers


def _capture_pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(
        fcm, "send_push_bg",
        lambda ids, title, body, data=None: sent.append(
            {"ids": sorted(ids), "title": title, "data": data or {}}
        ),
    )
    return sent


def test_report_pushes_to_org_admins(client, db_session, monkeypatch):
    sent = _capture_pushes(monkeypatch)
    admin_uni = make_user(db_session, role="admin")
    admin_school = make_user(db_session, role="admin", org_type="school")
    student = make_user(db_session, role="student")
    offender = make_user(db_session, role="student")

    resp = client.post(
        "/reports",
        json={"reported_user_id": offender.id, "reason": "spam"},
        headers=auth_headers(student),
    )
    assert resp.status_code == 201, resp.text

    assert len(sent) == 1
    # Свой админ получает, чужой (school) — нет. БД в сьюте общая, поэтому
    # проверяем членство, а не точный список.
    assert admin_uni.id in sent[0]["ids"]
    assert admin_school.id not in sent[0]["ids"]
    assert sent[0]["data"]["type"] == "admin_report"


def test_avatar_request_pushes_to_admins(client, db_session, monkeypatch):
    sent = _capture_pushes(monkeypatch)
    admin = make_user(db_session, role="admin")
    teacher = make_user(db_session, role="teacher")

    resp = client.post(
        "/avatars/me",
        json={"display_name": "Проф. Тест", "photo_url": "/uploads/x.jpg", "voice_sample_url": "/uploads/v.mp3"},
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 201, resp.text

    assert len(sent) == 1
    assert admin.id in sent[0]["ids"]
    assert sent[0]["data"]["type"] == "avatar_request"
