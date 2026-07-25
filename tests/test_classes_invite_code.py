import pytest

from crud import classes as crud_classes
from services.invite_codes import ALPHABET, CODE_LENGTH, legacy_deterministic_code, random_code
from routers.classes import _join_limiter
from tests.conftest import make_user, auth_headers


def legacy_client_formula(class_id: int) -> str:
    """Independent re-implementation of the Flutter/Nuxt client formula, kept
    separate from services.invite_codes so a regression there wouldn't hide
    behind testing the same code against itself."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    n = class_id * 1337 + 42
    code = ""
    for _ in range(6):
        code += alphabet[n % 32]
        n = n // 32 + class_id * 7
    return code


@pytest.mark.parametrize("class_id", [1, 7, 42, 123])
def test_legacy_deterministic_code_matches_client_formula(class_id):
    assert legacy_deterministic_code(class_id) == legacy_client_formula(class_id)


def test_random_code_uses_expected_alphabet_and_length():
    for _ in range(200):
        code = random_code()
        assert len(code) == CODE_LENGTH
        assert all(ch in ALPHABET for ch in code)
    assert "0" not in ALPHABET and "O" not in ALPHABET and "1" not in ALPHABET and "I" not in ALPHABET


def test_generate_unique_code_avoids_collisions(db_session, monkeypatch):
    teacher = make_user(db_session, role="teacher")
    existing = crud_classes.create_class(db_session, "Class A", None, created_by=teacher.id)

    calls = iter([existing.invite_code, existing.invite_code, "ZZZZZZ"])
    monkeypatch.setattr("services.invite_codes.random_code", lambda: next(calls))

    code = crud_classes.generate_unique_code(db_session)
    assert code == "ZZZZZZ"


def test_create_class_generates_unique_random_codes(db_session):
    teacher = make_user(db_session, role="teacher")
    codes = set()
    for i in range(15):
        c = crud_classes.create_class(db_session, f"Class {i}", None, created_by=teacher.id)
        assert len(c.invite_code) == CODE_LENGTH
        codes.add(c.invite_code)
    assert len(codes) == 15


def test_join_by_code_happy_path(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "Math", "desc", created_by=teacher.id)

    resp = client.post(
        "/api/classes/join-by-code",
        json={"code": cls.invite_code.lower()},  # lower-case + verifies normalization
        headers=auth_headers(student),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == cls.id
    assert body["name"] == "Math"

    members = client.get(f"/api/classes/{cls.id}/members", headers=auth_headers(teacher)).json()
    assert any(m["id"] == student.id for m in members)


def test_join_by_code_unknown_code_returns_404(client, db_session):
    student = make_user(db_session, role="student")
    resp = client.post(
        "/api/classes/join-by-code",
        json={"code": "ZZZZZZ"},
        headers=auth_headers(student),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "class_not_found"


def test_invite_code_hidden_from_student_visible_to_owner_and_admin(client, db_session):
    teacher = make_user(db_session, role="teacher")
    other_teacher = make_user(db_session, role="teacher")
    admin = make_user(db_session, role="admin")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "Physics", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls.id, student.id)

    r_student = client.get(f"/api/classes/{cls.id}", headers=auth_headers(student))
    assert r_student.json()["invite_code"] is None

    r_owner = client.get(f"/api/classes/{cls.id}", headers=auth_headers(teacher))
    assert r_owner.json()["invite_code"] == cls.invite_code

    r_admin = client.get(f"/api/classes/{cls.id}", headers=auth_headers(admin))
    assert r_admin.json()["invite_code"] == cls.invite_code

    # A teacher who doesn't own the class shouldn't see its code either.
    r_other = client.get(f"/api/classes/{cls.id}", headers=auth_headers(other_teacher))
    assert r_other.json()["invite_code"] is None

    # /classes/all never exposes the code, even to the owner.
    r_all = client.get("/api/classes/all", headers=auth_headers(teacher))
    assert all(c["invite_code"] is None for c in r_all.json())


def test_regenerate_code_changes_code_and_invalidates_old_one(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "Chemistry", None, created_by=teacher.id)
    old_code = cls.invite_code

    resp = client.post(f"/api/classes/{cls.id}/regenerate-code", headers=auth_headers(teacher))
    assert resp.status_code == 200
    new_code = resp.json()["invite_code"]
    assert new_code != old_code

    old_resp = client.post("/api/classes/join-by-code", json={"code": old_code}, headers=auth_headers(student))
    assert old_resp.status_code == 404

    new_resp = client.post("/api/classes/join-by-code", json={"code": new_code}, headers=auth_headers(student))
    assert new_resp.status_code == 200


def test_regenerate_code_forbidden_for_non_owner_non_admin(client, db_session):
    teacher = make_user(db_session, role="teacher")
    other_teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "History", None, created_by=teacher.id)

    resp = client.post(f"/api/classes/{cls.id}/regenerate-code", headers=auth_headers(other_teacher))
    assert resp.status_code == 403


def test_join_by_code_rate_limited(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "Biology", None, created_by=teacher.id)
    _join_limiter.reset(student.id)

    for _ in range(_join_limiter.max_calls):
        r = client.post("/api/classes/join-by-code", json={"code": "NOPE00"}, headers=auth_headers(student))
        assert r.status_code == 404

    r = client.post("/api/classes/join-by-code", json={"code": cls.invite_code}, headers=auth_headers(student))
    assert r.status_code == 429

    _join_limiter.reset(student.id)
