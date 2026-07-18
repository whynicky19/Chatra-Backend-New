from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db import get_db
import schemas
from crud import users as crud_users
from security import hash_password, verify_password, create_access_token, create_refresh_token, decode_refresh_token
from services.rate_limit import RateLimiter, client_ip
from services.otp import issue_code, verify_code
from services.mailer import send_code_email, otp_debug
from jose import JWTError
from deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Ключ IP+username: смена email не обходит лимит, соседей по NAT не блокируем зря.
_login_limiter = RateLimiter(max_calls=5, window_seconds=60)
# Запрос кодов (forgot-password / resend-verification): антиспам по IP+email.
_code_limiter = RateLimiter(max_calls=3, window_seconds=60, namespace="otp")


def normalize_email(email: str) -> str:
    """Email регистронезависим: приводим к нижнему регистру и обрезаем пробелы,
    иначе 'User@x.com' и 'user@x.com' стали бы разными аккаунтами и логин по
    другому регистру не находил бы пользователя."""
    return email.strip().lower()


def _norm_org(org_type: str) -> str:
    return org_type if org_type in ("university", "school") else "university"


def _issue_tokens(user) -> dict:
    return {
        "access_token": create_access_token(subject=str(user.id), token_version=user.token_version),
        "refresh_token": create_refresh_token(subject=str(user.id), token_version=user.token_version),
        "token_type": "bearer",
    }


@router.post("/register", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED)
def register(user: schemas.UserRegister, db: Session = Depends(get_db)):
    org_type = user.org_type if user.org_type in ("university", "school") else "university"
    email = normalize_email(user.email)
    existing = crud_users.get_user_by_email(db, email, org_type)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    hashed = hash_password(user.password)
    # Роль при самостоятельной регистрации всегда student:
    # teacher/admin выдаёт только администратор через /admin.
    try:
        created = crud_users.create_user(
            db,
            email,
            hashed,
            role="student",
            full_name=user.full_name,
            org_type=org_type,
        )
    except IntegrityError:
        # Гонка: параллельная регистрация того же (email, org_type) проскочила
        # проверку existing и упёрлась в составной unique-констрейнт.
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")

    # Новый аккаунт не подтверждён (ORM default), сразу шлём код на email.
    # Вход будет заблокирован до подтверждения (login → 403 email_not_verified).
    code = issue_code(db, email, org_type, "verify")
    send_code_email(email, code, "verify")
    return created


@router.post("/login", response_model=schemas.Token)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    org_type: str = Query("university"),
):
    email = normalize_email(form_data.username)
    rate_key = f"{client_ip(request)}:{email}"
    _login_limiter.check(rate_key, detail="Слишком много попыток входа. Подождите минуту.")
    user = crud_users.get_user_by_email(db, email, org_type)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="user_inactive")
    # Email не подтверждён — клиент по этому коду уводит на экран ввода кода.
    # Письмо здесь НЕ шлём (чтобы ретраи входа не спамили почту): код запросит
    # экран верификации через /auth/resend-verification.
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="email_not_verified")
    _login_limiter.reset(rate_key)
    return _issue_tokens(user)


@router.post("/refresh", response_model=schemas.Token)
def refresh_token(body: schemas.RefreshRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_refresh_token(body.refresh_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = crud_users.get_user_by_id(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    # Отзыв: refresh-токен старой версии (после logout/смены пароля) недействителен.
    if int(payload.get("tv", 0)) != user.token_version:
        raise HTTPException(status_code=401, detail="Token revoked")

    access_token = create_access_token(subject=str(user.id), token_version=user.token_version)
    new_refresh_token = create_refresh_token(subject=str(user.id), token_version=user.token_version)
    return {"access_token": access_token, "refresh_token": new_refresh_token, "token_type": "bearer"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Серверный отзыв всех токенов пользователя. Инкремент token_version делает
    недействительными все ранее выданные access/refresh на всех устройствах."""
    current_user.token_version += 1
    db.commit()


@router.post("/change-password", response_model=schemas.Token)
def change_password(
    body: schemas.ChangePassword,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="wrong_current_password")
    current_user.hashed_password = hash_password(body.new_password)
    # Отзываем все старые сессии и тут же выдаём свежую пару для текущего клиента,
    # чтобы юзера не выкинуло на экран входа после успешной смены пароля.
    current_user.token_version += 1
    db.commit()
    db.refresh(current_user)
    return _issue_tokens(current_user)


@router.post("/verify-email", response_model=schemas.Token)
def verify_email(body: schemas.VerifyEmailRequest, db: Session = Depends(get_db)):
    """Подтверждает email по коду из письма и сразу выдаёт токены (авто-вход)."""
    email = normalize_email(body.email)
    org_type = _norm_org(body.org_type)
    user = crud_users.get_user_by_email(db, email, org_type)
    if not user:
        raise HTTPException(status_code=400, detail="invalid_code")
    if user.is_verified:
        # Идемпотентность: уже подтверждён — просто впускаем.
        return _issue_tokens(user)
    if not verify_code(db, email, org_type, "verify", body.code):
        raise HTTPException(status_code=400, detail="invalid_code")
    user.is_verified = True
    db.commit()
    db.refresh(user)
    return _issue_tokens(user)


@router.post("/resend-verification")
def resend_verification(body: schemas.EmailCodeRequest, request: Request, db: Session = Depends(get_db)):
    """Повторно шлёт код подтверждения. Всегда 200 (не раскрываем существование
    аккаунта). dev_code возвращается только при OTP_DEBUG=1."""
    email = normalize_email(body.email)
    org_type = _norm_org(body.org_type)
    _code_limiter.check(f"{client_ip(request)}:{email}", detail="Слишком часто. Подождите минуту.")
    resp: dict = {"sent": False}
    user = crud_users.get_user_by_email(db, email, org_type)
    if user and not user.is_verified:
        code = issue_code(db, email, org_type, "verify")
        send_code_email(email, code, "verify")
        resp["sent"] = True
        if otp_debug():
            resp["dev_code"] = code
    return resp


@router.post("/forgot-password")
def forgot_password(body: schemas.EmailCodeRequest, request: Request, db: Session = Depends(get_db)):
    """Шлёт код для сброса пароля. Всегда 200 (анти-энумерация)."""
    email = normalize_email(body.email)
    org_type = _norm_org(body.org_type)
    _code_limiter.check(f"{client_ip(request)}:{email}", detail="Слишком часто. Подождите минуту.")
    resp: dict = {"sent": False}
    user = crud_users.get_user_by_email(db, email, org_type)
    if user and user.is_active:
        code = issue_code(db, email, org_type, "reset")
        send_code_email(email, code, "reset")
        resp["sent"] = True
        if otp_debug():
            resp["dev_code"] = code
    return resp


@router.post("/reset-password", response_model=schemas.Token)
def reset_password(body: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    """Сбрасывает пароль по коду из письма, отзывает старые сессии и впускает."""
    email = normalize_email(body.email)
    org_type = _norm_org(body.org_type)
    user = crud_users.get_user_by_email(db, email, org_type)
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="invalid_code")
    if not verify_code(db, email, org_type, "reset", body.code):
        raise HTTPException(status_code=400, detail="invalid_code")
    user.hashed_password = hash_password(body.new_password)
    # Успешный код из письма доказывает владение почтой — заодно подтверждаем email.
    user.is_verified = True
    # Отзываем все старые сессии (пароль изменился).
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return _issue_tokens(user)


@router.get("/me", response_model=schemas.UserResponse)
def me(current_user=Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=schemas.UserResponse)
def update_me(
    body: schemas.UpdateMe,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if body.full_name is not None:
        current_user.full_name = body.full_name.strip() or None

    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(
    body: schemas.DeleteAccount,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Самоудаление аккаунта (требование App Store / GDPR). Подтверждается
    паролем, чтобы украденный access-токен не позволил удалить аккаунт.
    Связанные записи (посты, чаты, сдачи и т.д.) удаляются каскадом на уровне
    ORM/БД — та же логика, что при удалении админом."""
    if not verify_password(body.password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="wrong_current_password")
    db.delete(current_user)
    db.commit()


def admin_required(current_user=Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin allowed")
    return current_user