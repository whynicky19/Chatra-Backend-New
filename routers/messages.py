import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from db import get_db
from schemas import MessageCreate
from deps import get_current_user
from permissions import require_chat_member, require_message_chat_member
from datetime import datetime, timezone

router = APIRouter(prefix="/messages", tags=["Messages"])
logger = logging.getLogger(__name__)

# SEC-9: детали внутренних ошибок (трассировки/структура БД) наружу не отдаём —
# логируем полностью, клиенту возвращаем обезличенный текст.
_INTERNAL_ERROR = "Внутренняя ошибка сервера"


def _safe_date(val) -> str | None:
    if val is None:
        return None
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    s = str(val)
    return s.replace(' ', 'T') if s else None


@router.post("/chat/{chat_id}")
def send_message(
    chat_id: int,
    msg: MessageCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_chat_member(db, chat_id, current_user.id)
    try:
        now = datetime.now(timezone.utc)
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        result = db.execute(
            text(
                "INSERT INTO messages (content, chat_id, user_id, created_at) "
                "VALUES (:content, :chat_id, :user_id, :created_at) RETURNING id"
            ),
            {
                "content": msg.content,
                "chat_id": chat_id,
                "user_id": current_user.id,
                "created_at": now_str,
            },
        )
        db.commit()
        new_id = result.scalar_one_or_none()
        return {
            "status": "sent",
            "id": new_id,
            "content": msg.content,
            "chat_id": chat_id,
            "user_id": current_user.id,
            "created_at": now.isoformat(),
            "is_read": False,
            "file_url": None,
        }
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("messages error")
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.get("/chat/{chat_id}")
def get_messages(
    chat_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_chat_member(db, chat_id, current_user.id)
    try:
        result = db.execute(
            text(
                "SELECT id, content, chat_id, user_id, created_at, "
                "COALESCE(is_read, false) as is_read, file_url "
                "FROM messages WHERE chat_id = :cid ORDER BY id"
            ),
            {"cid": chat_id},
        ).fetchall()

        return [
            {
                "id": r[0],
                "content": r[1],
                "chat_id": r[2],
                "user_id": r[3],
                "created_at": _safe_date(r[4]),
                "is_read": bool(r[5]) if r[5] is not None else False,
                "file_url": r[6],
            }
            for r in result
        ]
    except HTTPException:
        raise
    except Exception:
        logger.exception("messages error")
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.delete("/{message_id}")
def delete_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    msg = db.execute(
        text("SELECT id, user_id FROM messages WHERE id = :id"), {"id": message_id}
    ).fetchone()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg[1] != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    try:
        db.execute(text("DELETE FROM messages WHERE id = :id"), {"id": message_id})
        db.commit()
        return {"status": "deleted"}
    except Exception:
        db.rollback()
        logger.exception("messages error")
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.put("/{message_id}/read")
def mark_read(
    message_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_message_chat_member(db, message_id, current_user.id)
    try:
        db.execute(
            text("UPDATE messages SET is_read = 1 WHERE id = :id"),
            {"id": message_id},
        )
        db.commit()
        return {"status": "read"}
    except Exception:
        db.rollback()
        logger.exception("messages error")
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.get("/unread")
def unread_messages(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        result = db.execute(
            text(
                "SELECT m.id, m.content, m.chat_id, m.user_id, m.created_at "
                "FROM messages m "
                "JOIN chat_members cm ON cm.chat_id = m.chat_id AND cm.user_id = :uid "
                "WHERE m.user_id != :uid AND COALESCE(m.is_read, 0) = 0 "
                "ORDER BY m.id"
            ),
            {"uid": current_user.id},
        ).fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "chat_id": r[2],
                "user_id": r[3],
                "created_at": _safe_date(r[4]),
            }
            for r in result
        ]
    except Exception:
        logger.exception("messages error")
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)
