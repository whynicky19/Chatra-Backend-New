"""Пуши админам: заявка на аватар должна уведомлять админов
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


def test_avatar_request_pushes_to_admins(client, db_session, monkeypatch):
    sent = _capture_pushes(monkeypatch)
    admin = make_user(db_session, role="admin")
    teacher = make_user(db_session, role="teacher")

    resp = client.post(
        "/api/avatars/me",
        json={"display_name": "Проф. Тест", "photo_url": "/uploads/x.jpg", "voice_sample_url": "/uploads/v.mp3"},
        headers=auth_headers(teacher),
    )
    assert resp.status_code == 201, resp.text

    assert len(sent) == 1
    assert admin.id in sent[0]["ids"]
    assert sent[0]["data"]["type"] == "avatar_request"
