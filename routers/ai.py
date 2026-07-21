import json
import os
import httpx
from typing import List, Optional, Union, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from deps import get_current_user
from db import get_db
from services.rate_limit import RateLimiter
from services import ai_quota
from sqlalchemy.orm import Session

from models import AiMessage

router = APIRouter(prefix="/ai", tags=["AI"])

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"   # supports vision


_ai_limiter = RateLimiter(max_calls=20, window_seconds=60)

def _check_rate_limit(user_id: int):
    _ai_limiter.check(
        user_id,
        detail="Слишком много запросов к ИИ. Подождите немного и попробуйте снова.",
    )


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]]


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    max_tokens: int = 2000
    temperature: float = 0.7
    class_id: Optional[int] = None
    lecture_context: Optional[str] = None


class ChatResponse(BaseModel):
    content: str
    # Состояние дневной квоты после этого сообщения — чтобы клиент обновлял
    # счётчик без дополнительного запроса к /ai/limits.
    quota: Optional[dict] = None


def _serialize_message(m: ChatMessage) -> dict:

    if isinstance(m.content, str):
        return {"role": m.role, "content": m.content}

    return {"role": m.role, "content": m.content}


def _content_to_text(content: Union[str, List[Any]]) -> str:
    """Плоский текст для хранения истории. Vision-контент (список) сводим к
    текстовым сегментам — картинки в сохранённую историю не тащим."""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def _persist_ai_exchange(
    db: Session, user_id: int, class_id: Optional[int],
    messages: List[ChatMessage], assistant_content: str,
) -> None:
    """Сохраняем дельту переписки: последнее сообщение пользователя + ответ
    ассистента. Клиент всегда шлёт всю историю, поэтому пишем только новые две
    строки — дублей нет. Ошибки хранения не должны ломать ответ ИИ."""
    try:
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"), None
        )
        if last_user is not None:
            db.add(AiMessage(
                user_id=user_id, class_id=class_id, role="user",
                content=_content_to_text(last_user.content),
            ))
        db.add(AiMessage(
            user_id=user_id, class_id=class_id, role="assistant",
            content=assistant_content,
        ))
        db.commit()
    except Exception:
        db.rollback()


@router.post("/chat", response_model=ChatResponse)
async def ai_chat(
    body: ChatRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="AI service is not configured. Please set OPENAI_API_KEY on the server.",
        )

    _check_rate_limit(current_user.id)
    # Дневная квота сообщений на пользователя (общая для приложения и сайта).
    ai_quota.enforce(db, current_user)

    if not body.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")

    max_tokens = min(body.max_tokens, 4000)


    has_vision = any(
        isinstance(m.content, list) for m in body.messages
    )
    model = OPENAI_MODEL

    payload = {
        "model": model,
        "messages": [_serialize_message(m) for m in body.messages],
        "max_tokens": max_tokens,
        "temperature": body.temperature,
    }

    if body.lecture_context:
        payload["messages"].insert(0, {
            "role": "system",
            "content": f"Материалы класса (отвечай опираясь на них):\n{body.lecture_context[:8000]}",
        })

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                OPENAI_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI service timed out")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"AI service unreachable: {e}")

    if not resp.is_success:
        try:
            err = resp.json()
            msg = err.get("error", {}).get("message", f"OpenAI error {resp.status_code}")
        except Exception:
            msg = f"OpenAI error {resp.status_code}"
        raise HTTPException(status_code=502, detail=msg)

    data = resp.json()
    content = data["choices"][0]["message"]["content"]


    # ВАЖНО: эта же строка — счётчик дневной квоты (services/ai_quota.py),
    # поэтому при сбое откатываем сессию, а не оставляем её сломанной.
    try:
        usage = data.get("usage", {})
        from models import AiUsageLog
        log = AiUsageLog(
            user_id=current_user.id,
            class_id=body.class_id,
            endpoint="chat_vision" if has_vision else "chat",
            org_type=current_user.org_type,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()

    # Персистим переписку на сервер — история синхронизируется между устройствами.
    _persist_ai_exchange(db, current_user.id, body.class_id, body.messages, content)

    return ChatResponse(content=content, quota=ai_quota.quota_status(db, current_user))


class AiLimitsResponse(BaseModel):
    limit: int
    used: int
    remaining: Optional[int] = None   # None — безлимит
    unlimited: bool
    resets_at: str


@router.get("/limits", response_model=AiLimitsResponse)
def get_ai_limits(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Дневная квота сообщений ИИ текущего пользователя (для счётчика в UI)."""
    return AiLimitsResponse(**ai_quota.quota_status(db, current_user))


class AiMessageResponse(BaseModel):
    role: str
    content: str
    created_at: Optional[str] = None


class AiHistoryImportItem(BaseModel):
    role: str
    content: str


class AiHistoryImport(BaseModel):
    class_id: Optional[int] = None
    messages: List[AiHistoryImportItem]


def _history_query(db: Session, user_id: int, class_id: Optional[int]):
    q = db.query(AiMessage).filter(AiMessage.user_id == user_id)
    # class_id NULL (глобальный экран) и конкретный класс — разные треды.
    if class_id is None:
        q = q.filter(AiMessage.class_id.is_(None))
    else:
        q = q.filter(AiMessage.class_id == class_id)
    return q


@router.get("/history", response_model=List[AiMessageResponse])
def get_ai_history(
    class_id: Optional[int] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = _history_query(db, current_user.id, class_id).order_by(AiMessage.id).all()
    return [
        AiMessageResponse(
            role=r.role, content=r.content,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@router.delete("/history")
def clear_ai_history(
    class_id: Optional[int] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _history_query(db, current_user.id, class_id).delete(synchronize_session=False)
    db.commit()
    return {"cleared": True}


@router.post("/history/import", response_model=List[AiMessageResponse])
def import_ai_history(
    body: AiHistoryImport,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Разовая миграция локальной истории на сервер: льём ТОЛЬКО если серверный
    тред пуст, иначе возвращаем существующий (идемпотентно, без дублей)."""
    existing = _history_query(db, current_user.id, body.class_id).order_by(AiMessage.id).all()
    if not existing:
        for m in body.messages:
            if m.role not in ("user", "assistant"):
                continue
            db.add(AiMessage(
                user_id=current_user.id, class_id=body.class_id,
                role=m.role, content=m.content,
            ))
        db.commit()
        existing = _history_query(db, current_user.id, body.class_id).order_by(AiMessage.id).all()
    return [
        AiMessageResponse(
            role=r.role, content=r.content,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in existing
    ]
