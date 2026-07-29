import logging
import os
import httpx
from datetime import datetime
from typing import List, Optional, Union, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from deps import get_current_user
from db import get_db
from services.rate_limit import RateLimiter
from services import ai_quota
from sqlalchemy.orm import Session

from models import AiMessage, AiThread
from utils.time import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI"])

DEFAULT_THREAD_TITLE = "Новый чат"

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
    # Обязателен для главного ассистента (class_id IS NULL). Для репетитора
    # класса (class_id задан) полностью игнорируется.
    thread_id: Optional[int] = None
    lecture_context: Optional[str] = None


class ChatResponse(BaseModel):
    content: str
    # Состояние дневной квоты после этого сообщения — чтобы клиент обновлял
    # счётчик без дополнительного запроса к /ai/limits.
    quota: Optional[dict] = None
    # Текущий заголовок треда (в т.ч. только что автосгенерированный) — только
    # для пути главного ассистента; None для репетитора класса.
    thread_title: Optional[str] = None


class AiThreadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


class AiThreadUpdate(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


def _get_owned_thread(db: Session, user_id: int, thread_id: int) -> AiThread:
    """Тред текущего пользователя или 404 (не раскрываем существование чужих)."""
    thread = (
        db.query(AiThread)
        .filter(AiThread.id == thread_id, AiThread.user_id == user_id)
        .first()
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Тред не найден")
    return thread


def _serialize_message(m: ChatMessage) -> dict:
    # Строку и vision-список (list of parts) OpenAI принимает как есть.
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
    db: Session, user_id: int, class_id: Optional[int], thread_id: Optional[int],
    messages: List[ChatMessage], assistant_content: str,
) -> None:
    """Сохраняем дельту переписки: последнее сообщение пользователя + ответ
    ассистента. Клиент всегда шлёт всю историю, поэтому пишем только новые две
    строки — дублей нет. Ошибки хранения не должны ломать ответ ИИ.
    thread_id задан только для главного ассистента; у репетитора класса — None."""
    try:
        last_user = next(
            (m for m in reversed(messages) if m.role == "user"), None
        )
        if last_user is not None:
            db.add(AiMessage(
                user_id=user_id, class_id=class_id, thread_id=thread_id, role="user",
                content=_content_to_text(last_user.content),
            ))
        db.add(AiMessage(
            user_id=user_id, class_id=class_id, thread_id=thread_id, role="assistant",
            content=assistant_content,
        ))
        db.commit()
    except Exception:
        db.rollback()


def _log_ai_usage(db: Session, user, class_id, endpoint: str, usage: dict) -> None:
    """Пишем строку расхода токенов. Ошибка логирования не должна ломать ответ."""
    try:
        from models import AiUsageLog
        db.add(AiUsageLog(
            user_id=user.id,
            class_id=class_id,
            endpoint=endpoint,
            org_type=user.org_type,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        ))
        db.commit()
    except Exception:
        db.rollback()


async def _generate_thread_title(api_key: str, user_text: str) -> tuple[str, dict]:
    """Короткий заголовок треда из первого сообщения пользователя. Возвращает
    (заголовок, usage). Бросает исключение при сбое — вызывающий его глушит."""
    prompt_text = (user_text or "").strip()[:500]
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Summarize the user's message into a short chat title "
                    "(3-6 words, no quotes, no trailing punctuation), in the "
                    "same language the user wrote in."
                ),
            },
            {"role": "user", "content": prompt_text},
        ],
        "max_tokens": 20,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OPENAI_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=payload,
        )
    if not resp.is_success:
        raise RuntimeError(f"title OpenAI error {resp.status_code}")
    data = resp.json()
    raw = (data["choices"][0]["message"]["content"] or "").strip()
    title = raw.strip().strip('"').strip("'").strip()
    title = title[:120]
    if not title:
        raise RuntimeError("empty title")
    return title, data.get("usage", {})


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

    # Путь главного ассистента (class_id IS NULL) требует существующий тред,
    # принадлежащий пользователю. Путь репетитора класса thread_id игнорирует.
    thread: Optional[AiThread] = None
    is_first_exchange = False
    if body.class_id is None:
        if body.thread_id is None:
            raise HTTPException(
                status_code=400,
                detail="thread_id обязателен для главного ассистента",
            )
        thread = _get_owned_thread(db, current_user.id, body.thread_id)
        is_first_exchange = (
            db.query(AiMessage.id)
            .filter(AiMessage.thread_id == thread.id)
            .first()
            is None
        )

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
            "content": (
                "Материалы класса (отвечай опираясь на них). Каждый материал "
                "помечен заголовком вида \"### Лекция N: <тема>\" — если "
                "пользователь просит объяснить материал по номеру (например "
                "\"объясни 2 лекцию\"), найди блок с этим номером и объясняй "
                "именно его содержание. Объясняй подробно: раскрывай ключевые "
                "понятия, приводи примеры и логику, а не просто пересказывай "
                "заголовки. Математические формулы записывай в LaTeX: "
                "инлайн — \\(...\\), блочные — \\[...\\]. Код — в блоках ```язык.\n\n"
                f"{body.lecture_context[:8000]}"
            ),
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
    _log_ai_usage(
        db, current_user, body.class_id,
        "chat_vision" if has_vision else "chat", data.get("usage", {}),
    )

    # Персистим переписку на сервер — история синхронизируется между устройствами.
    thread_id = thread.id if thread is not None else None
    _persist_ai_exchange(
        db, current_user.id, body.class_id, thread_id, body.messages, content
    )

    thread_title: Optional[str] = None
    if thread is not None:
        # Активность треда двигает updated_at (сортировка сайдбара), метаданные —
        # нет (см. PATCH). Обновляем после сохранения сообщений.
        try:
            thread.updated_at = utcnow()
            db.commit()
        except Exception:
            db.rollback()

        # Автозаголовок только для первого обмена в треде. Синхронно, чтобы
        # клиент сразу показал имя. Любой сбой не должен ломать основной ответ.
        if is_first_exchange:
            try:
                last_user = next(
                    (m for m in reversed(body.messages) if m.role == "user"), None
                )
                first_text = _content_to_text(last_user.content) if last_user else ""
                new_title, title_usage = await _generate_thread_title(api_key, first_text)
                thread.title = new_title
                db.commit()
                # Расход токенов заголовка — endpoint НЕ из CHAT_ENDPOINTS,
                # значит не считается в дневную квоту сообщений.
                _log_ai_usage(db, current_user, body.class_id, "ai_title", title_usage)
            except Exception as e:
                db.rollback()
                logger.warning("ai: не удалось сгенерировать заголовок треда (%s)", e)
        thread_title = thread.title

    return ChatResponse(
        content=content,
        quota=ai_quota.quota_status(db, current_user),
        thread_title=thread_title,
    )


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
    thread_id: Optional[int] = None
    messages: List[AiHistoryImportItem]


def _history_query(
    db: Session, user_id: int, class_id: Optional[int], thread_id: Optional[int] = None,
):
    q = db.query(AiMessage).filter(AiMessage.user_id == user_id)
    if class_id is None:
        # Главный ассистент: тред задаётся thread_id (проверка владения — выше).
        q = q.filter(AiMessage.class_id.is_(None), AiMessage.thread_id == thread_id)
    else:
        # Репетитор класса: поведение как раньше — тред = (user_id, class_id).
        q = q.filter(AiMessage.class_id == class_id)
    return q


# ── AI threads (только главный ассистент, class_id IS NULL) ─────────────────────

@router.get("/threads", response_model=List[AiThreadResponse])
def list_ai_threads(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Треды текущего пользователя: закреплённые сверху, затем по свежести."""
    rows = (
        db.query(AiThread)
        .filter(AiThread.user_id == current_user.id)
        .order_by(AiThread.pinned.desc(), AiThread.updated_at.desc())
        .all()
    )
    return rows


@router.post("/threads", response_model=AiThreadResponse)
def create_ai_thread(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    thread = AiThread(user_id=current_user.id, title=DEFAULT_THREAD_TITLE)
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


@router.patch("/threads/{thread_id}", response_model=AiThreadResponse)
def update_ai_thread(
    thread_id: int,
    body: AiThreadUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Правка метаданных треда. Обновляет только переданные поля. updated_at НЕ
    трогаем: он привязан к активности сообщений (сортировка сайдбара), а не к
    переименованию/закреплению — иначе pin поднимал бы тред как «свежий»."""
    thread = _get_owned_thread(db, current_user.id, thread_id)
    if body.title is not None:
        thread.title = body.title.strip()[:120] or DEFAULT_THREAD_TITLE
    if body.pinned is not None:
        thread.pinned = body.pinned
    db.commit()
    db.refresh(thread)
    return thread


@router.delete("/threads/{thread_id}")
def delete_ai_thread(
    thread_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Удаляет тред. Его сообщения уходят каскадом (FK ondelete=CASCADE)."""
    thread = _get_owned_thread(db, current_user.id, thread_id)
    db.delete(thread)
    db.commit()
    return {"deleted": True}


@router.get("/history", response_model=List[AiMessageResponse])
def get_ai_history(
    class_id: Optional[int] = None,
    thread_id: Optional[int] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if class_id is None:
        if thread_id is None:
            raise HTTPException(
                status_code=400,
                detail="thread_id обязателен для главного ассистента",
            )
        _get_owned_thread(db, current_user.id, thread_id)
    rows = (
        _history_query(db, current_user.id, class_id, thread_id)
        .order_by(AiMessage.id)
        .all()
    )
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
    thread_id: Optional[int] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if class_id is None:
        if thread_id is None:
            raise HTTPException(
                status_code=400,
                detail="thread_id обязателен для главного ассистента",
            )
        _get_owned_thread(db, current_user.id, thread_id)
    _history_query(db, current_user.id, class_id, thread_id).delete(
        synchronize_session=False
    )
    db.commit()
    return {"cleared": True}


@router.post("/history/import", response_model=List[AiMessageResponse])
def import_ai_history(
    body: AiHistoryImport,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Разовая миграция локальной истории на сервер: льём ТОЛЬКО если серверный
    тред пуст, иначе возвращаем существующий (идемпотентно, без дублей).
    Для главного ассистента импорт скоуплен на конкретный thread_id."""
    if body.class_id is None:
        if body.thread_id is None:
            raise HTTPException(
                status_code=400,
                detail="thread_id обязателен для главного ассистента",
            )
        _get_owned_thread(db, current_user.id, body.thread_id)
    existing = (
        _history_query(db, current_user.id, body.class_id, body.thread_id)
        .order_by(AiMessage.id)
        .all()
    )
    if not existing:
        for m in body.messages:
            if m.role not in ("user", "assistant"):
                continue
            db.add(AiMessage(
                user_id=current_user.id, class_id=body.class_id,
                thread_id=body.thread_id if body.class_id is None else None,
                role=m.role, content=m.content,
            ))
        db.commit()
        existing = (
            _history_query(db, current_user.id, body.class_id, body.thread_id)
            .order_by(AiMessage.id)
            .all()
        )
    return [
        AiMessageResponse(
            role=r.role, content=r.content,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in existing
    ]
