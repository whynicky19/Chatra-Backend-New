"""Состояние уведомлений (прочитано/скрыто) — серверная истина, чтобы бейджи и
прочитанное совпадали в приложении и на сайте. Сам список уведомлений клиенты
по-прежнему собирают из заданий/оценок; сюда пишется только read/dismissed по
каноническому ключу '{kind}:{ref_id}' (kind: assignment|deadline|grade)."""
from typing import List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db import get_db
from deps import get_current_user
from models import NotificationState
from pydantic import BaseModel, Field
from typing import Annotated

router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Колонка notification_states.notif_key — String(64). Postgres эту длину
# ЖЁСТКО проверяет (SQLite — нет): более длинный ключ от клиента доезжал до
# INSERT и падал StringDataRightTruncation, то есть 500 на рядовом запросе.
# Отбиваем валидацией на входе (понятный 422), а не исключением драйвера.
# Реальный ключ всегда короткий: '{kind}:{ref_id}'.
NOTIF_KEY_MAX_LEN = 64
NotifKey = Annotated[str, Field(min_length=1, max_length=NOTIF_KEY_MAX_LEN)]
# «Прочитать всё» приходит списком от клиента: без границы это неограниченное
# число upsert'ов в одном запросе.
MAX_READ_ALL_KEYS = 500


class NotifStateResponse(BaseModel):
    notif_key: str
    read: bool
    dismissed: bool


class NotifStateUpdate(BaseModel):
    notif_key: NotifKey
    read: Optional[bool] = None
    dismissed: Optional[bool] = None


class NotifReadAll(BaseModel):
    keys: Annotated[List[NotifKey], Field(max_length=MAX_READ_ALL_KEYS)]


def _get_or_create(db: Session, user_id: int, notif_key: str) -> NotificationState:
    row = (
        db.query(NotificationState)
        .filter(
            NotificationState.user_id == user_id,
            NotificationState.notif_key == notif_key,
        )
        .first()
    )
    if row is None:
        row = NotificationState(user_id=user_id, notif_key=notif_key)
        db.add(row)
    return row


@router.get("/state", response_model=List[NotifStateResponse])
def get_states(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(NotificationState)
        .filter(NotificationState.user_id == current_user.id)
        .all()
    )
    return [
        NotifStateResponse(notif_key=r.notif_key, read=r.read, dismissed=r.dismissed)
        for r in rows
    ]


@router.post("/state", response_model=NotifStateResponse)
def set_state(
    body: NotifStateUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upsert одного уведомления: ставит переданные поля (read/dismissed)."""
    row = _get_or_create(db, current_user.id, body.notif_key)
    if body.read is not None:
        row.read = body.read
    if body.dismissed is not None:
        row.dismissed = body.dismissed
    db.commit()
    db.refresh(row)
    return NotifStateResponse(notif_key=row.notif_key, read=row.read, dismissed=row.dismissed)


@router.post("/read-all")
def read_all(
    body: NotifReadAll,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пометить прочитанными пачкой (переданные ключи)."""
    for key in body.keys:
        row = _get_or_create(db, current_user.id, key)
        row.read = True
    db.commit()
    return {"read": len(body.keys)}
