from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from db import get_db
import schemas
from crud import users as crud_users
from security import hash_password, verify_password, create_access_token, create_refresh_token, decode_refresh_token
from services.rate_limit import RateLimiter, client_ip
from jose import JWTError
from deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Ключ IP+username: смена email не обходит лимит, соседей по NAT не блокируем зря.
_login_limiter = RateLimiter(max_calls=5, window_seconds=60)


@router.post("/register", response_model=schemas.UserResponse, status_code=status.HTTP_201_CREATED)
def register(user: schemas.UserRegister, db: Session = Depends(get_db)):
    org_type = user.org_type if user.org_type in ("university", "school") else "university"
    existing = crud_users.get_user_by_email(db, user.email, org_type)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    hashed = hash_password(user.password)
    # Роль при самостоятельной регистрации всегда student:
    # teacher/admin выдаёт только администратор через /admin.
    created = crud_users.create_user(
        db,
        user.email,
        hashed,
        role="student",
        full_name=user.full_name,
        org_type=org_type,
    )
    return created


@router.post("/login", response_model=schemas.Token)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    org_type: str = Query("university"),
):
    rate_key = f"{client_ip(request)}:{form_data.username}"
    _login_limiter.check(rate_key, detail="Слишком много попыток входа. Подождите минуту.")
    user = crud_users.get_user_by_email(db, form_data.username, org_type)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    _login_limiter.reset(rate_key)
    access_token = create_access_token(subject=str(user.id))
    refresh_token = create_refresh_token(subject=str(user.id))
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}


@router.post("/refresh", response_model=schemas.Token)
def refresh_token(body: schemas.RefreshRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_refresh_token(body.refresh_token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    from crud import users as crud_users
    user = crud_users.get_user_by_id(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    access_token = create_access_token(subject=str(user.id))
    new_refresh_token = create_refresh_token(subject=str(user.id))
    return {"access_token": access_token, "refresh_token": new_refresh_token, "token_type": "bearer"}


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


def admin_required(current_user=Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin allowed")
    return current_user