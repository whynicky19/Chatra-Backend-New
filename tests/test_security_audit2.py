"""Регрессионные тесты второго аудита безопасности (SEC-1, SEC-2, SEC-4..SEC-6):
подписанные файлы, владение классом/заданием/оценкой,
рейтинг без PII, file-text по подписи."""
import os

from crud import classes as cc
from crud import assignments as crud_assignments
from services.file_urls import make_signature, sign_upload_url
from tests.conftest import make_user, auth_headers


def _assignment(db, teacher):
    cls = cc.create_class(db, "C", None, created_by=teacher.id)
    a = crud_assignments.create_assignment(
        db=db, class_id=cls.id, title="H", description=None,
        criteria=[{"name": "x", "weight": 100}], max_score=100,
        deadline=None, created_by=teacher.id,
    )
    # см. test_cross_class_access.py — после flush() нужен явный commit.
    db.commit()
    return cls, a


# ── SEC-1: файлы отдаются только по подписи ──────────────────────────────────

def test_uploads_require_valid_signature(client):
    d = os.getenv("UPLOAD_DIR", "uploads")
    os.makedirs(d, exist_ok=True)
    name = "sec1probe.txt"
    with open(os.path.join(d, name), "w") as f:
        f.write("secret-body")

    # без подписи файл недоступен (обязательные exp/sig отсутствуют)
    assert client.get(f"/api/uploads/{name}").status_code in (403, 422)

    exp, sig = make_signature(name)
    ok = client.get(f"/api/uploads/{name}", params={"exp": exp, "sig": sig})
    assert ok.status_code == 200 and ok.text == "secret-body"

    # подделанная подпись
    assert client.get(f"/api/uploads/{name}", params={"exp": exp, "sig": "deadbeef"}).status_code == 403
    # просроченная подпись
    exp2, sig2 = make_signature(name, ttl=-10)
    assert client.get(f"/api/uploads/{name}", params={"exp": exp2, "sig": sig2}).status_code == 403


def test_uploads_path_traversal_blocked(client):
    exp, sig = make_signature("../security.py")
    r = client.get("/api/uploads/../security.py", params={"exp": exp, "sig": sig})
    assert r.status_code in (403, 404)


def test_json_upload_urls_are_signed(client, db_session):
    teacher = make_user(db_session, role="teacher")
    cls = cc.create_class(
        db_session, "C", None, created_by=teacher.id,
        cover_image="http://localhost:8000/uploads/coverX.png",
    )
    r = client.get(f"/api/classes/{cls.id}", headers=auth_headers(teacher))
    assert r.status_code == 200
    assert "/uploads/coverX.png?exp=" in r.text and "sig=" in r.text


# ── SEC-2: владение классом/заданием/оценкой ─────────────────────────────────

def test_non_owner_teacher_cannot_modify_class(client, db_session):
    owner = make_user(db_session, role="teacher")
    other = make_user(db_session, role="teacher")
    cls = cc.create_class(db_session, "C", None, created_by=owner.id)

    assert client.put(f"/api/classes/{cls.id}", json={"name": "X"}, headers=auth_headers(other)).status_code == 403
    assert client.delete(f"/api/classes/{cls.id}", headers=auth_headers(other)).status_code == 403
    assert client.put(f"/api/classes/{cls.id}", json={"name": "X"}, headers=auth_headers(owner)).status_code == 200


def test_non_owner_teacher_cannot_modify_or_grade_assignment(client, db_session):
    owner = make_user(db_session, role="teacher")
    other = make_user(db_session, role="teacher")
    student = make_user(db_session)
    cls, a = _assignment(db_session, owner)
    cc.add_member(db_session, cls.id, student.id)

    assert client.put(f"/api/assignments/{a.id}", json={"title": "Z"}, headers=auth_headers(other)).status_code == 403
    assert client.delete(f"/api/assignments/{a.id}", headers=auth_headers(other)).status_code == 403

    sub = client.post(f"/api/assignments/{a.id}/submit", json={"text_content": "ans"}, headers=auth_headers(student))
    assert sub.status_code == 201
    sid = sub.json()["id"]
    assert client.post(f"/api/submissions/{sid}/grade", json={"score": 50}, headers=auth_headers(other)).status_code == 403
    assert client.post(f"/api/submissions/{sid}/grade", json={"score": 50}, headers=auth_headers(owner)).status_code == 201


# ── SEC-4: рейтинг без e-mail + проверка членства ────────────────────────────

def test_rating_requires_membership_and_hides_email(client, db_session):
    owner = make_user(db_session, role="teacher")
    student = make_user(db_session)
    outsider = make_user(db_session)
    cls, _ = _assignment(db_session, owner)
    cc.add_member(db_session, cls.id, student.id)

    assert client.get(f"/api/classes/{cls.id}/rating", headers=auth_headers(outsider)).status_code == 403
    r = client.get(f"/api/classes/{cls.id}/rating", headers=auth_headers(student))
    assert r.status_code == 200
    assert "@example.com" not in r.text

    # глобальный рейтинг тоже не раскрывает e-mail
    r = client.get("/api/rating", headers=auth_headers(student))
    assert r.status_code == 200 and "@example.com" not in r.text


def test_members_requires_membership(client, db_session):
    owner = make_user(db_session, role="teacher")
    outsider = make_user(db_session)
    cls, _ = _assignment(db_session, owner)
    assert client.get(f"/api/classes/{cls.id}/members", headers=auth_headers(outsider)).status_code == 403


# ── SEC-6: file-text только по подписи ───────────────────────────────────────

def test_file_text_requires_signature(client, db_session):
    user = make_user(db_session)
    unsigned = "http://localhost:8000/uploads/whatever.pdf"
    assert client.get("/api/upload/utils/file-text", params={"url": unsigned}, headers=auth_headers(user)).status_code == 403

    signed = sign_upload_url(unsigned)
    r = client.get("/api/upload/utils/file-text", params={"url": signed}, headers=auth_headers(user))
    assert r.status_code == 200  # подпись валидна; текст пуст (файла нет)


def test_sign_uploads_in_text_signs_relative_urls():
    """Приложение сохраняет вложения относительной ссылкой «/uploads/x.png».
    Без подписи прокси отдавал 422 — фото не открывалось ни в приложении, ни
    на сайте. Проверяем, что относительные ссылки тоже подписываются, а чужие
    хосты не трогаются."""
    import json
    import re
    from services.file_urls import sign_uploads_in_text, verify_signature

    body = json.dumps({
        "bare": "/uploads/x.png",
        "markdown": "🖼️ [Фото](/uploads/z.png) — ceo.png",
        "foreign": "https://cdn.example.com/uploads/foreign.png",
    }, ensure_ascii=False)

    out = json.loads(sign_uploads_in_text(body))

    for key in ("bare", "markdown"):
        m = re.search(r"/uploads/([^?)\s\"]+)\?exp=(\d+)&sig=(\w+)", out[key])
        assert m, out[key]
        assert verify_signature(m.group(1), m.group(2), m.group(3))
    assert out["foreign"] == "https://cdn.example.com/uploads/foreign.png"
