import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, Query
from starlette.websockets import WebSocketDisconnect
from sqlalchemy.orm import Session
from sqlalchemy import text
from db import SessionLocal
from security import decode_token
from crud import users as crud_users

logger = logging.getLogger(__name__)

router = APIRouter()

connections: dict[int, list[WebSocket]] = {}

MAX_MESSAGE_LENGTH = 4000

async def _authenticate(token: str, chat_id: int) -> int | None:
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub", 0))
        if not user_id:
            return None
        db: Session = SessionLocal()
        try:
            user = crud_users.get_user_by_id(db, user_id)
            if not user or not user.is_active:
                return None
            row = db.execute(
                text("SELECT 1 FROM chat_members WHERE chat_id = :cid AND user_id = :uid"),
                {"cid": chat_id, "uid": user_id},
            ).fetchone()
            if not row:
                return None
            return user_id
        finally:
            db.close()
    except Exception:
        return None


def _save_message(chat_id: int, user_id: int, content: str) -> dict | None:
    """Сохраняет сообщение в БД и возвращает нормализованный объект для рассылки."""
    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        result = db.execute(
            text(
                "INSERT INTO messages (content, chat_id, user_id, created_at) "
                "VALUES (:content, :chat_id, :user_id, :created_at) RETURNING id"
            ),
            {
                "content": content,
                "chat_id": chat_id,
                "user_id": user_id,
                "created_at": now.strftime('%Y-%m-%d %H:%M:%S'),
            },
        )
        db.commit()
        new_id = result.scalar_one_or_none()
        return {
            "type": "message",
            "id": new_id,
            "content": content,
            "chat_id": chat_id,
            "user_id": user_id,
            "created_at": now.isoformat(),
            "is_read": False,
            "file_url": None,
        }
    except Exception:
        db.rollback()
        logger.exception("Не удалось сохранить WS-сообщение (chat_id=%s)", chat_id)
        return None
    finally:
        db.close()


def _parse_incoming(raw: str, chat_id: int, user_id: int) -> dict | None:
    """Валидирует входящий JSON. user_id всегда берётся из токена —
    любые id из payload игнорируются. Некорректный payload → None (игнор)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    msg_type = data.get("type", "message")

    if msg_type == "typing":
        return {"type": "typing", "chat_id": chat_id, "user_id": user_id}

    if msg_type == "message":
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return {"type": "message", "content": content.strip()[:MAX_MESSAGE_LENGTH]}

    return None


async def _broadcast(chat_id: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    dead = []
    for conn in connections.get(chat_id, []):
        try:
            await conn.send_text(raw)
        except Exception:
            dead.append(conn)
    for d in dead:
        try:
            connections[chat_id].remove(d)
        except ValueError:
            pass


@router.websocket("/ws/{chat_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    chat_id: int,
    token: str = Query(...),
):
    user_id = await _authenticate(token, chat_id)
    if not user_id:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    connections.setdefault(chat_id, []).append(websocket)

    try:
        while True:
            raw = await websocket.receive_text()

            parsed = _parse_incoming(raw, chat_id, user_id)
            if parsed is None:
                continue

            if parsed["type"] == "typing":
                await _broadcast(chat_id, parsed)
                continue

            saved = _save_message(chat_id, user_id, parsed["content"])
            if saved:
                await _broadcast(chat_id, saved)
                try:
                    from services.fcm import notify_chat_message

                    notify_chat_message(chat_id, user_id, None, parsed["content"])
                except Exception:
                    pass

    except WebSocketDisconnect:
        try:
            connections[chat_id].remove(websocket)
        except (ValueError, KeyError):
            pass
