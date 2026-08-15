"""Регистрация FCM-токенов устройств для push-уведомлений.

Клиент после логина/получения токена шлёт POST /push/register; при выходе —
POST /push/unregister. Токен уникален глобально: если то же устройство раньше
принадлежало другому аккаунту, запись переезжает на текущего пользователя."""
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db import get_db
from deps import get_current_user
from models import DeviceToken
from utils.time import utcnow
from pydantic import BaseModel, Field

router = APIRouter(prefix="/push", tags=["Push"])


class TokenRegister(BaseModel):
    token: str
    # device_tokens.platform — String(16): в Postgres более длинное значение
    # роняет INSERT (StringDataRightTruncation → 500), поэтому режем на входе.
    platform: Optional[str] = Field(default=None, max_length=16)  # android | ios


class TokenUnregister(BaseModel):
    token: str


@router.post("/register")
def register_token(
    body: TokenRegister,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upsert по token: привязывает токен к текущему юзеру и обновляет last_seen."""
    token = (body.token or "").strip()
    if not token:
        return {"status": "ignored"}

    row = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if row is None:
        row = DeviceToken(
            user_id=current_user.id,
            token=token,
            platform=body.platform,
        )
        db.add(row)
    else:
        row.user_id = current_user.id
        row.platform = body.platform or row.platform
        row.last_seen = utcnow()
    db.commit()
    return {"status": "ok"}


@router.post("/unregister")
def unregister_token(
    body: TokenUnregister,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет токен (при logout). Чужой токен не трогаем — только свой."""
    token = (body.token or "").strip()
    if token:
        db.query(DeviceToken).filter(
            DeviceToken.token == token,
            DeviceToken.user_id == current_user.id,
        ).delete(synchronize_session=False)
        db.commit()
    return {"status": "ok"}
