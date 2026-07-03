import os
import tempfile

_test_db_fd, _test_db_path = tempfile.mkstemp(suffix=".db")
os.close(_test_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("REFRESH_SECRET_KEY", "test-refresh-secret-key")

import pytest
from fastapi.testclient import TestClient

import main  # noqa: E402  (import after env vars are set so db.py picks up the test DB)
from db import SessionLocal
from crud import users as crud_users
from security import hash_password, create_access_token


@pytest.fixture()
def client():
    # Instantiated without `with` so the app's startup lifespan (background
    # deadline-checker loop) never starts during tests.
    return TestClient(main.app)


@pytest.fixture()
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_counter = {"n": 0}


def make_user(db, role="student", org_type="university"):
    _counter["n"] += 1
    email = f"user{_counter['n']}@example.com"
    hashed = hash_password("password123")
    return crud_users.create_user(db, email, hashed, role=role, org_type=org_type)


def auth_headers(user):
    token = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}
