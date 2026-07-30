"""Модерация UGC: жалобы, очередь модерации.

Контракт со клиентом — lib/services/api_service.dart (раздел «Модерация UGC»).
Требование сторов: App Store Guideline 1.2, Google Play UGC.
"""
from tests.conftest import auth_headers, make_user

from crud import posts as crud_posts
from models import Report


# ── Жалобы ────────────────────────────────────────────────────────────────
def test_report_created_and_duplicate_is_409(client, db_session):
    author = make_user(db_session, role="teacher")
    reporter = make_user(db_session)
    post = crud_posts.create_new_post(db_session, "Плохой пост", "текст", author.id)

    r = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": post.id, "reason": "abuse",
              "comment": "оскорбления"},
        headers=auth_headers(reporter),
    )
    assert r.status_code == 201, r.text
    report_id = r.json()["id"]

    # Повторная жалоба того же юзера на тот же объект — 409 report_already,
    # клиент показывает «вы уже жаловались», а не ошибку.
    r2 = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": post.id, "reason": "spam"},
        headers=auth_headers(reporter),
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "report_already"

    saved = db_session.query(Report).filter(Report.id == report_id).first()
    assert saved.reason == "abuse" and saved.resolved is False
    # created_at — naive UTC, как остальные даты API.
    assert saved.created_at.tzinfo is None


def test_report_unknown_target_is_404(client, db_session):
    reporter = make_user(db_session)
    r = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": 999999, "reason": "spam"},
        headers=auth_headers(reporter),
    )
    assert r.status_code == 404


def test_report_bad_enum_is_422(client, db_session):
    reporter = make_user(db_session)
    r = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": 1, "reason": "whatever"},
        headers=auth_headers(reporter),
    )
    assert r.status_code == 422


def test_report_requires_auth(client):
    r = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": 1, "reason": "spam"},
    )
    assert r.status_code == 401


def test_ai_message_report_accepted_without_target_check(client, db_session):
    """Переписка с ИИ приватна, серверного id у сообщения нет — жалоба
    принимается без проверки существования объекта."""
    reporter = make_user(db_session)
    r = client.post(
        "/api/reports",
        json={"target_type": "ai_message", "target_id": 12345, "reason": "inappropriate"},
        headers=auth_headers(reporter),
    )
    assert r.status_code == 201


# ── Очередь модерации (admin) ─────────────────────────────────────────────
def test_admin_queue_and_resolve(client, db_session):
    admin = make_user(db_session, role="admin")
    author = make_user(db_session, role="teacher")
    reporter = make_user(db_session)
    reporter.full_name = "Иван Петров"
    db_session.commit()
    post = crud_posts.create_new_post(db_session, "Жалоба на это", "x", author.id)

    created = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": post.id, "reason": "abuse",
              "comment": "текст жалобы"},
        headers=auth_headers(reporter),
    ).json()["id"]

    r = client.get("/api/admin/reports?open=true", headers=auth_headers(admin))
    assert r.status_code == 200
    item = next(i for i in r.json() if i["id"] == created)
    assert item["reporter_name"] == "Иван Петров"
    assert item["reporter_email"] == reporter.email
    assert item["resolved"] is False
    # naive ISO без таймзоны (lib/utils/dates.dart::parseServerDate).
    assert "+" not in item["created_at"] and not item["created_at"].endswith("Z")

    r = client.post(
        f"/api/admin/reports/{created}/resolve",
        json={"action": "content_removed"},
        headers=auth_headers(admin),
    )
    assert r.status_code == 200 and r.json()["resolved"] is True

    open_ids = [i["id"] for i in client.get(
        "/api/admin/reports?open=true", headers=auth_headers(admin)
    ).json()]
    assert created not in open_ids


def test_admin_queue_forbidden_for_non_admin(client, db_session):
    student = make_user(db_session)
    assert client.get(
        "/api/admin/reports?open=true", headers=auth_headers(student)
    ).status_code == 403
    assert client.post(
        "/api/admin/reports/1/resolve", json={}, headers=auth_headers(student)
    ).status_code == 403


def test_admin_queue_isolated_by_org(client, db_session):
    school_admin = make_user(db_session, role="admin", org_type="school")
    author = make_user(db_session, role="teacher")
    reporter = make_user(db_session)
    post = crud_posts.create_new_post(db_session, "Универский пост", "x", author.id)
    created = client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": post.id, "reason": "spam"},
        headers=auth_headers(reporter),
    ).json()["id"]

    ids = [i["id"] for i in client.get(
        "/api/admin/reports?open=true", headers=auth_headers(school_admin)
    ).json()]
    assert created not in ids


def test_report_rate_limited(client, db_session):
    """~10 жалоб в час на пользователя — защита модератора от спама."""
    reporter = make_user(db_session)
    author = make_user(db_session, role="teacher")
    codes = []
    for i in range(12):
        post = crud_posts.create_new_post(db_session, f"Пост {i}", "x", author.id)
        codes.append(
            client.post(
                "/api/reports",
                json={"target_type": "post", "target_id": post.id, "reason": "spam"},
                headers=auth_headers(reporter),
            ).status_code
        )
    assert codes[:10] == [201] * 10
    assert 429 in codes[10:]
