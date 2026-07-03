"""Общие проверки прав доступа к объектам (защита от IDOR).

Единое место для SQL-проверок членства в чатах и классах —
роутеры не должны дублировать эти запросы.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from models import Chat, class_members


def require_chat_member(db: Session, chat_id: int, user_id: int) -> None:
    """404 если чата нет, 403 если пользователь не участник."""
    chat = db.query(Chat).filter(Chat.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    row = db.execute(
        text("SELECT 1 FROM chat_members WHERE chat_id = :cid AND user_id = :uid"),
        {"cid": chat_id, "uid": user_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this chat")


def require_message_chat_member(db: Session, message_id: int, user_id: int):
    """404 если сообщения нет, 403 если пользователь не участник его чата.
    Возвращает строку (id, chat_id)."""
    msg = db.execute(
        text("SELECT id, chat_id FROM messages WHERE id = :id"), {"id": message_id}
    ).fetchone()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    require_chat_member(db, msg[1], user_id)
    return msg


def is_class_member(db: Session, class_id: int, user_id: int) -> bool:
    row = db.execute(
        class_members.select().where(
            class_members.c.class_id == class_id,
            class_members.c.user_id == user_id,
        )
    ).fetchone()
    return row is not None


def require_class_access(db: Session, class_id: int, current_user) -> None:
    """Студент должен состоять в классе; teacher/admin проходят по org-проверке роутера."""
    if current_user.role in ("teacher", "admin"):
        return
    if not is_class_member(db, class_id, current_user.id):
        raise HTTPException(status_code=403, detail="Вы не состоите в этом классе")
