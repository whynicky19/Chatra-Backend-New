import logging
from fastapi import APIRouter, Depends, HTTPException, Query
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
async def send_message(
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
        payload = {
            "type": "message",
            "id": new_id,
            "content": msg.content,
            "chat_id": chat_id,
            "user_id": current_user.id,
            "created_at": now.isoformat(),
            "is_read": False,
            "file_url": None,
        }
        # Сообщения раньше сохранялись только через REST, а рассылка по WS
        # была только у сообщений, присланных сырым фреймом в /ws/{chat_id} —
        # в реальности этим путём никто не пользуется (и сайт, и приложение
        # шлют через REST), поэтому получатели узнавали о новом сообщении
        # только на следующем поллинге. Рассылаем и здесь.
        try:
            from websocket import broadcast

            await broadcast(chat_id, payload)
        except Exception:  # noqa: BLE001
            pass
        try:
            from services.fcm import notify_chat_message

            notify_chat_message(chat_id, current_user.id, current_user.full_name, msg.content)
        except Exception:  # noqa: BLE001
            pass
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
    limit: int | None = Query(None, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """FE-1: keyset-пагинация истории чата. limit опционален (совместимость:
    без него отдаём всю историю в ASC, как раньше). С limit — последние `limit`
    сообщений; при скролле вверх клиент передаёт id самого верхнего сообщения
    как before_id и получает предыдущую порцию. Внутри выбираем DESC по id и
    разворачиваем в ASC для отображения."""
    require_chat_member(db, chat_id, current_user.id)
    try:
        if limit is None and before_id is None:
            # Легаси-путь: вся история по возрастанию id.
            result = db.execute(
                text(
                    "SELECT id, content, chat_id, user_id, created_at, "
                    "COALESCE(is_read, false) as is_read, file_url "
                    "FROM messages WHERE chat_id = :cid ORDER BY id"
                ),
                {"cid": chat_id},
            ).fetchall()
            rows = list(result)
        else:
            params = {"cid": chat_id, "limit": limit or 50}
            before_clause = ""
            if before_id is not None:
                before_clause = "AND id < :before_id "
                params["before_id"] = before_id
            result = db.execute(
                text(
                    "SELECT id, content, chat_id, user_id, created_at, "
                    "COALESCE(is_read, false) as is_read, file_url "
                    f"FROM messages WHERE chat_id = :cid {before_clause}"
                    "ORDER BY id DESC LIMIT :limit"
                ),
                params,
            ).fetchall()
            rows = list(reversed(result))  # ASC для показа
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
            for r in rows
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
