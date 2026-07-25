"""Тесты продуктовых auth-флоу: нормализация email, уникальность по (email,
org_type), верификация email кодом, восстановление пароля, отзыв токенов
(logout / смена пароля), удаление аккаунта, блокировка."""
from security import decode_token
from tests.conftest import make_user


def _register(client, email, password="password123", org_type="university", full_name="Иванов Иван"):
    return client.post("/api/auth/register", json={
        "email": email, "password": password, "full_name": full_name, "org_type": org_type,
    })


def _login(client, email, password="password123", org_type="university"):
    return client.post(
        "/api/auth/login",
        params={"org_type": org_type},
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _register_and_verify(client, email, password="password123", org_type="university"):
    """Регистрирует и подтверждает email через код (OTP_DEBUG отдаёт dev_code)."""
    assert _register(client, email, password, org_type).status_code == 201
    code = client.post("/api/auth/resend-verification",
                       json={"email": email, "org_type": org_type}).json()["dev_code"]
    r = client.post("/api/auth/verify-email", json={"email": email, "org_type": org_type, "code": code})
    assert r.status_code == 200, r.text
    return r.json()


# ── Нормализация email ───────────────────────────────────────────────────────

def test_register_normalizes_email_and_login_is_case_insensitive(client):
    _register_and_verify(client, "MiXeD@Case.COM")
    r = _login(client, "mixed@case.com")
    assert r.status_code == 200, r.text
    # Повторная регистрация того же email в другом регистре → 409.
    assert _register(client, "mixed@CASE.com").status_code == 409


# ── Уникальность по (email, org_type) ────────────────────────────────────────

def test_same_email_allowed_across_org_types(client):
    assert _register(client, "dup@x.com", org_type="university").status_code == 201
    assert _register(client, "dup@x.com", org_type="school").status_code == 201
    assert _register(client, "dup@x.com", org_type="university").status_code == 409


# ── Верификация email ────────────────────────────────────────────────────────

def test_login_blocked_until_email_verified(client):
    assert _register(client, "nv@x.com").status_code == 201
    blocked = _login(client, "nv@x.com")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "email_not_verified"
    # После верификации вход открыт.
    _register_and_verify  # noqa (документируем связь)
    code = client.post("/api/auth/resend-verification", json={"email": "nv@x.com"}).json()["dev_code"]
    assert client.post("/api/auth/verify-email", json={"email": "nv@x.com", "code": code}).status_code == 200
    assert _login(client, "nv@x.com").status_code == 200


def test_verify_email_wrong_code_rejected(client):
    assert _register(client, "wc@x.com").status_code == 201
    bad = client.post("/api/auth/verify-email", json={"email": "wc@x.com", "code": "000000"})
    assert bad.status_code == 400
    assert bad.json()["detail"] == "invalid_code"


def test_verify_returns_tokens_and_carries_token_version(client):
    tokens = _register_and_verify(client, "vt@x.com")
    assert decode_token(tokens["access_token"])["tv"] == 0
    # Токены из verify-email сразу рабочие.
    assert client.get("/api/auth/me", headers=_bearer(tokens["access_token"])).status_code == 200


# ── Восстановление пароля ────────────────────────────────────────────────────

def test_forgot_and_reset_password(client):
    _register_and_verify(client, "fp@x.com")
    old = _login(client, "fp@x.com").json()
    dev = client.post("/api/auth/forgot-password", json={"email": "fp@x.com"}).json()
    assert dev["sent"] is True
    code = dev["dev_code"]

    r = client.post("/api/auth/reset-password",
                    json={"email": "fp@x.com", "code": code, "new_password": "brandnew123"})
    assert r.status_code == 200

    # Старый пароль не подходит, новый — да.
    assert _login(client, "fp@x.com", "password123").status_code == 401
    assert _login(client, "fp@x.com", "brandnew123").status_code == 200
    # Старые сессии отозваны (token_version инкрементнулся).
    assert client.get("/api/auth/me", headers=_bearer(old["access_token"])).status_code == 401


def test_forgot_password_unknown_email_returns_200(client):
    # Анти-энумерация: нет утечки существования аккаунта.
    r = client.post("/api/auth/forgot-password", json={"email": "nobody@x.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is False


def test_reset_password_invalid_code_rejected(client):
    _register_and_verify(client, "ric@x.com")
    r = client.post("/api/auth/reset-password",
                    json={"email": "ric@x.com", "code": "999999", "new_password": "whatever12"})
    assert r.status_code == 400


# ── Смена пароля отзывает старые токены ───────────────────────────────────────

def test_change_password_revokes_old_token_and_issues_new(client):
    login = _register_and_verify(client, "cp@x.com")
    old_access = login["access_token"]

    bad = client.post("/api/auth/change-password",
                      json={"current_password": "wrong", "new_password": "newpassword1"},
                      headers=_bearer(old_access))
    assert bad.status_code == 400
    assert bad.json()["detail"] == "wrong_current_password"

    ok = client.post("/api/auth/change-password",
                     json={"current_password": "password123", "new_password": "newpassword1"},
                     headers=_bearer(old_access))
    assert ok.status_code == 200
    new_access = ok.json()["access_token"]

    assert client.get("/api/auth/me", headers=_bearer(old_access)).status_code == 401
    assert client.get("/api/auth/me", headers=_bearer(new_access)).status_code == 200
    assert _login(client, "cp@x.com", "password123").status_code == 401
    assert _login(client, "cp@x.com", "newpassword1").status_code == 200


# ── Logout отзывает refresh на сервере ────────────────────────────────────────

def test_logout_revokes_refresh_token(client):
    login = _register_and_verify(client, "lo@x.com")
    access, refresh = login["access_token"], login["refresh_token"]

    assert client.post("/api/auth/logout", headers=_bearer(access)).status_code == 204
    assert client.get("/api/auth/me", headers=_bearer(access)).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh}).status_code == 401


# ── Удаление аккаунта ─────────────────────────────────────────────────────────

def test_delete_account_requires_password_and_removes_user(client):
    access = _register_and_verify(client, "del@x.com")["access_token"]

    bad = client.request("DELETE", "/api/auth/me", json={"password": "wrong"}, headers=_bearer(access))
    assert bad.status_code == 400

    ok = client.request("DELETE", "/api/auth/me", json={"password": "password123"}, headers=_bearer(access))
    assert ok.status_code == 204
    assert client.get("/api/auth/me", headers=_bearer(access)).status_code == 401
    assert _login(client, "del@x.com").status_code == 401


# ── Блокировка аккаунта ───────────────────────────────────────────────────────

def test_blocked_user_gets_403_user_inactive(client, db_session):
    user = make_user(db_session, org_type="university")
    from security import create_access_token
    token = create_access_token(subject=str(user.id), token_version=user.token_version)
    user.is_active = False
    db_session.commit()

    me = client.get("/api/auth/me", headers=_bearer(token))
    assert me.status_code == 403
    assert me.json()["detail"] == "user_inactive"

    r = _login(client, user.email)
    assert r.status_code == 403
    assert r.json()["detail"] == "user_inactive"
