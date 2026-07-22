"""Дневной лимит сообщений ИИ на пользователя (как у Claude/ChatGPT).

По умолчанию 50 сообщений в сутки на аккаунт; окно — календарные сутки UTC,
счётчик обнуляется в 00:00 UTC. Источник правды — таблица `ai_usage_logs`
(строка пишется только после успешного ответа модели), поэтому лимит:

  * общий для приложения и сайта — бэкенд один;
  * переживает рестарт и работает при нескольких воркерах (в отличие от
    RateLimiter, который без REDIS_URL живёт в памяти процесса);
  * не сбрасывается очисткой истории чата (в отличие от `ai_messages`).

Настройки окружения:
  AI_DAILY_MESSAGE_LIMIT — сообщений в сутки на пользователя (0 = без лимита).

Освобождены от лимита: админы и пользователи с флагом `ai_unlimited`.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import AiUsageLog
from utils.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_DAILY_MESSAGE_LIMIT = 50

# Эндпоинты, которые считаются «сообщением к ИИ». ИИ-проверка работ
# ("ai-grade") сюда не входит — у неё свой бюджет (services/ai_budget.py).
CHAT_ENDPOINTS = ("chat", "chat_vision")


def daily_message_limit() -> int:
    try:
        return int(os.getenv("AI_DAILY_MESSAGE_LIMIT", str(DEFAULT_DAILY_MESSAGE_LIMIT)))
    except ValueError:
        return DEFAULT_DAILY_MESSAGE_LIMIT


def _day_start() -> datetime:
    """Начало текущих суток UTC (naive — как хранится created_at)."""
    now = utcnow()
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).replace(tzinfo=None)


def _day_end() -> datetime:
    return _day_start() + timedelta(days=1)


def messages_used_today(db: Session, user_id: int) -> int:
    return int(
        db.query(func.count(AiUsageLog.id))
        .filter(
            AiUsageLog.user_id == user_id,
            AiUsageLog.endpoint.in_(CHAT_ENDPOINTS),
            AiUsageLog.created_at >= _day_start(),
        )
        .scalar()
        or 0
    )


def is_exempt(user) -> bool:
    return bool(getattr(user, "ai_unlimited", False)) or getattr(user, "role", "") == "admin"


def quota_status(db: Session, user) -> dict:
    """Состояние квоты для UI: сколько осталось и когда обнулится."""
    limit = daily_message_limit()
    unlimited = is_exempt(user) or limit <= 0
    used = 0
    if not unlimited:
        try:
            used = messages_used_today(db, user.id)
        except Exception as e:
            logger.warning("ai_quota: не удалось посчитать сообщения (%s)", e)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if not unlimited else None,
        "unlimited": unlimited,
        "resets_at": _day_end().replace(tzinfo=timezone.utc).isoformat(),
    }


def enforce(db: Session, user) -> None:
    """429, если пользователь исчерпал дневной лимит сообщений."""
    limit = daily_message_limit()
    if limit <= 0 or is_exempt(user):
        return
    try:
        used = messages_used_today(db, user.id)
    except Exception as e:
        # Сбой подсчёта не должен ломать чат — логируем и пускаем.
        logger.warning("ai_quota: не удалось посчитать сообщения (%s)", e)
        return
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Дневной лимит ИИ исчерпан ({limit} сообщений в сутки). "
                "Лимит обновится после 00:00 UTC."
            ),
            headers={
                "X-AI-Quota-Limit": str(limit),
                "X-AI-Quota-Remaining": "0",
                "X-AI-Quota-Reset": _day_end().replace(tzinfo=timezone.utc).isoformat(),
            },
        )
