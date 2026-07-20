"""Блок-лист пользователя (UGC-модерация, App Store Guideline 1.2).

Блокировка должна быть доступной и обратимой, а также переживать переустановку
приложения и синхронизироваться между устройствами — поэтому список хранится на
сервере, а не в локальном хранилище клиента.

Имена отдаём вместе со списком, чтобы клиент мог отрисовать экран
«Заблокированные» без дополнительных запросов.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from deps import get_current_user
from models import User, UserBlock

router = APIRouter(prefix="/blocks", tags=["Blocks"])


class BlockedUser(BaseModel):
    user_id: int
    name: str


def _display_name(u: User | None, uid: int) -> str:
    if u is None:
        # Пользователь удалён, но запись блокировки ещё жива — не роняем экран.
        return f"#{uid}"
    return u.full_name or (u.email.split("@")[0] if u.email else f"#{uid}")


@router.get("", response_model=List[BlockedUser])
def list_blocks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Кого заблокировал текущий пользователь."""
    rows = (
        db.query(UserBlock, User)
        .outerjoin(User, User.id == UserBlock.blocked_user_id)
        .filter(UserBlock.user_id == current_user.id)
        .order_by(UserBlock.created_at.desc())
        .all()
    )
    return [
        BlockedUser(user_id=b.blocked_user_id, name=_display_name(u, b.blocked_user_id))
        for b, u in rows
    ]


@router.post("/{user_id}", status_code=201)
def block_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Заблокировать пользователя. Идемпотентно: повторный вызов не ошибка."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot_block_self")
    if not db.query(User.id).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="user_not_found")

    existing = (
        db.query(UserBlock)
        .filter(
            UserBlock.user_id == current_user.id,
            UserBlock.blocked_user_id == user_id,
        )
        .first()
    )
    if existing is None:
        db.add(UserBlock(user_id=current_user.id, blocked_user_id=user_id))
        db.commit()
    return {"blocked": True, "user_id": user_id}


@router.delete("/{user_id}")
def unblock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Разблокировать. Идемпотентно: снятие несуществующего блока — не ошибка."""
    db.query(UserBlock).filter(
        UserBlock.user_id == current_user.id,
        UserBlock.blocked_user_id == user_id,
    ).delete(synchronize_session=False)
    db.commit()
    return {"blocked": False, "user_id": user_id}
