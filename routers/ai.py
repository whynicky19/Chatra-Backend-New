import logging
import os
import time
import httpx
from datetime import datetime
from typing import List, Optional, Union, Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from deps import get_current_user
from db import get_db
from services.rate_limit import RateLimiter
from services import ai_quota
from sqlalchemy.orm import Session

from models import AiMessage, AiThread, Class as ClassModel
from permissions import require_class_access
from utils.time import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI"])

DEFAULT_THREAD_TITLE = "Новый чат"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"   # supports vision


# Потолок текста материалов класса в system-сообщении тьютора. Клиент
# собирает до 12 лекций (см. class_detail_screen._recomputeAiContext:
# до 4000 символов контента + до 5000 на каждый прикреплённый файл) — этот
# блок легко перерастает несколько десятков тысяч символов. Старый лимит
# 8000 обрезал материалы уже на 1-2-й лекции ПОСЕРЕДИНЕ блока: если студент
# просил "объясни лекцию 3", а лекции 1-2 суммарно занимали больше 8000
# символов, лекция 3 полностью выпадала из промпта, и модель либо
# придумывала ответ, либо отвечала не по контексту — притом без единой
# ошибки, молча. Резать нужно по границе "### Лекция", а не посимвольно.
MAX_LECTURE_CONTEXT_CHARS = 60_000


def _truncate_lecture_context(text: str, limit: int = MAX_LECTURE_CONTEXT_CHARS) -> str:
    """Обрезает материалы класса, не разрывая блок лекции посередине.

    Если текст короче лимита — возвращает как есть. Иначе ищет последнюю
    границу "### " перед лимитом и режет по ней (целиком отбрасывая
    последнюю неполную лекцию), с явной пометкой для модели, что часть
    материалов не поместилась — чтобы она не молчала и не выдумывала.
    """
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    boundary = truncated.rfind("\n\n### ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return (
        truncated
        + "\n\n[Часть материалов предмета не поместилась в контекст и здесь "
        "не показана — если пользователь спрашивает про лекцию, которой "
        "нет выше, прямо скажи, что не видишь её в текущем контексте, "
        "вместо того чтобы выдумывать содержание.]"
    )


_ai_limiter = RateLimiter(max_calls=20, window_seconds=60)

def _check_rate_limit(user_id: int):
    _ai_limiter.check(
        user_id,
        detail="Слишком много запросов к ИИ. Подождите немного и попробуйте снова.",
    )


# Кнопка «Остановить» в клиенте рвёт HTTP-соединение через CancelToken, но
# это не гарантированно прерывает соединение на сервере (keep-alive сокет
# может просто повиснуть, не сообщая о разрыве) — request.is_disconnected()
# в таком случае ничего не замечает. Клиент вдобавок явно шлёт /chat/cancel
# с тем же request_id отдельным запросом; ai_chat перед сохранением сверяется
# с этим реестром. Однопроцессный in-memory словарь — как и RateLimiter выше.
_CANCEL_TTL_SECONDS = 180  # с запасом перекрывает 90s таймаут запроса к OpenAI
_cancelled_requests: dict[str, float] = {}


def _mark_cancelled(request_id: str) -> None:
    now = time.time()
    _cancelled_requests[request_id] = now
    stale = [k for k, ts in _cancelled_requests.items() if now - ts > _CANCEL_TTL_SECONDS]
    for k in stale:
        _cancelled_requests.pop(k, None)


def _pop_cancelled(request_id: Optional[str]) -> bool:
    if not request_id:
        return False
    return _cancelled_requests.pop(request_id, None) is not None


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
    # DEPRECATED: раньше клиент (Flutter) сам собирал текст ВСЕХ лекций
    # класса и слал его целиком сюда — сервер вставлял его в системный
    # промпт без проверки. Теперь материалы класса ищутся и собираются на
    # СЕРВЕРЕ через RAG (services/rag_search.py, векторный поиск по
    # rag_chunks) — поле оставлено в схеме только чтобы старые версии
    # приложения не падали на валидации, но сервер его больше НЕ читает и
    # никак не использует (см. ai_chat: класс-контекст строится всегда
    # заново, без доверия клиенту).
    lecture_context: Optional[str] = None
    # Клиентский идентификатор запроса — используется только для отмены
    # (см. /chat/cancel и _pop_cancelled), на сам ответ ИИ не влияет.
    request_id: Optional[str] = None


class CancelChatRequest(BaseModel):
    request_id: str


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


def _check_class_access(db: Session, class_id: int, current_user) -> None:
    """Репетитор класса (class_id задан) раньше не проверял вообще ничего:
    любой авторизованный пользователь мог слать class_id чужого/несуществующего
    класса и это тихо писалось в ai_messages/ai_usage_logs с этим class_id —
    искажая аналитику расхода токенов по классам и подсовывая произвольный
    class_id в историю чужого (или несуществующего) класса. lecture_context
    целиком приходит от клиента, поэтому реального утечки чужих материалов
    тут не было — но доступ на класс всё равно должен подтверждаться, как и
    во всех остальных class-scoped ручках (см. routers/classes.py)."""
    cls = db.query(ClassModel).filter(ClassModel.id == class_id).first()
    if not cls or cls.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Предмет не найден")
    require_class_access(db, class_id, current_user)


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


@router.post("/chat/cancel")
async def ai_chat_cancel(
    body: CancelChatRequest,
    current_user=Depends(get_current_user),
):
    """Клиент шлёт это сразу после нажатия «Стоп» — отдельным запросом, не
    через тот же (уже отменяемый) CancelToken, иначе он тоже не дошёл бы."""
    _mark_cancelled(body.request_id)
    return {"ok": True}


@router.post("/chat", response_model=ChatResponse)
async def ai_chat(
    request: Request,
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
    # Класс-контекст, собранный СЕРВЕРОМ через RAG (см. ниже) — не путать с
    # body.lecture_context (deprecated, приходит от клиента и игнорируется).
    server_lecture_context = ""
    requested_lecture_number: Optional[int] = None
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
    else:
        _check_class_access(db, body.class_id, current_user)
        # RAG вместо client-side lecture_context: ищем релевантные чанки по
        # ПОСЛЕДНЕМУ вопросу пользователя (не по всей истории — история
        # может увести embedding-запрос в сторону от текущего вопроса) и
        # собираем контекст на сервере. Сбой поиска (нет OPENAI_API_KEY для
        # эмбеддингов, недоступен pgvector и Python-фолбэк одновременно и
        # т.п.) не должен ронять чат — репетитор просто останется без
        # материалов класса на этот раз, как было бы при пустом классе.
        try:
            last_user_msg = next(
                (m for m in reversed(body.messages) if m.role == "user"), None
            )
            query_text = _content_to_text(last_user_msg.content) if last_user_msg else ""
            from services import rag_search
            chunks, requested_lecture_number = await rag_search.search_class_materials(
                db, body.class_id, current_user.org_type, query_text,
            )
            server_lecture_context = rag_search.assemble_context(db, body.class_id, chunks)
        except Exception as e:
            logger.warning("ai_chat: не удалось собрать RAG-контекст класса %s (%s)", body.class_id, e)
            server_lecture_context = ""
            requested_lecture_number = None

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

    # Клиент рендерит LaTeX через flutter_math_fork, который регистрирует
    # только окружения aligned/alignedat/cases — align, align*, gather,
    # gathered, equation, eqnarray он не разбирает вообще и падает в сырой
    # LaTeX-текст на экране (см. lib/screens/ai/widgets/ai_message_content.dart).
    # Модель тянется к align*/equation по привычке из обычного LaTeX, поэтому
    # формулировка ниже действует всегда, а не только в контексте лекции —
    # у главного ассистента (без lecture_context) раньше не было вообще
    # никакого system-сообщения, форматирование формул было непредсказуемым.
    math_instruction = (
        "Математические формулы записывай в LaTeX: инлайн — \\(...\\), "
        "блочные — \\[...\\]. Внутри формул НЕ используй окружения align, "
        "align*, gather, gathered, equation, eqnarray — клиент их не "
        "поддерживает. Если в формуле несколько строк (перенос через \\\\), "
        "ОБЯЗАТЕЛЬНО оборачивай их в aligned: "
        "\\[\\begin{aligned} ... \\\\ ... \\end{aligned}\\] — перенос строк "
        "через \\\\ вне такого окружения невалиден и не отрендерится. "
        "Код — в блоках ```язык."
    )
    if server_lecture_context:
        payload["messages"].insert(0, {
            "role": "system",
            "content": (
                "Материалы предмета, найденные поиском по вопросу пользователя "
                "(отвечай опираясь на них — это не все материалы предмета, а "
                "только релевантные фрагменты). Каждый материал помечен "
                "заголовком вида \"### Лекция N: <тема>\" — если пользователь "
                "просит объяснить материал по номеру (например \"объясни 2 "
                "лекцию\"), объясняй именно содержимое этого блока. Доверяй "
                "содержимому фрагментов, а не своим общим знаниям — если "
                "показанный текст ЯВНО не по теме заголовка/описания лекции "
                "(например это другой документ, код, инструкция), прямо "
                "скажи об этом в начале ответа, а не притворяйся, что "
                "объясняешь заявленную тему по несуществующему материалу. "
                "Объясняй подробно: раскрывай ключевые понятия, приводи "
                f"примеры и логику, а не просто пересказывай заголовки. {math_instruction}\n\n"
                f"{_truncate_lecture_context(server_lecture_context)}"
            ),
        })
    elif body.class_id is not None and requested_lecture_number is not None:
        # Явно попросили конкретную лекцию, которой нет (номер вне
        # диапазона либо материалы ещё не проиндексировались фоном) — не
        # молчим и не подсовываем контекст другой лекции вместо запрошенной.
        payload["messages"].insert(0, {"role": "system", "content": (
            f"У этого предмета нет лекции №{requested_lecture_number} (или её "
            "материалы ещё индексируются — попробуйте через минуту). Прямо "
            f"скажи об этом пользователю. {math_instruction}"
        )})
    elif body.class_id is not None:
        # Класс существует и доступен, но по вопросу ничего релевantного не
        # нашлось (в классе ещё нет материалов / они не проиндексированы).
        payload["messages"].insert(0, {"role": "system", "content": (
            "В материалах предмета не нашлось ничего по этому вопросу (либо "
            "материалов ещё нет, либо они ещё индексируются) — прямо скажи "
            f"об этом, не выдумывай ответ по общим знаниям. {math_instruction}"
        )})
    else:
        payload["messages"].insert(0, {"role": "system", "content": math_instruction})

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

    # Пользователь мог нажать «Стоп», пока мы ждали ответ модели — клиентский
    # CancelToken рвёт HTTP-соединение, но сам запрос к OpenAI это не
    # прерывает (см. await client.post выше). is_disconnected() ловит явный
    # разрыв соединения; _pop_cancelled — явный сигнал от клиента (см.
    # /chat/cancel) на случай, когда соединение просто повисло, а не
    # закрылось. Без этой проверки ответ на отменённый вопрос всё равно уходил
    # в ai_messages и «всплывал» при следующем открытии чата (или при
    # переключении между чатами в истории) — синхронизация истории просто
    # подтягивала его с сервера.
    if await request.is_disconnected() or _pop_cancelled(body.request_id):
        return ChatResponse(
            content=content,
            quota=ai_quota.quota_status(db, current_user),
            thread_title=None,
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
