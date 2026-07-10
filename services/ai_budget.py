"""BE-9: дневной бюджет токенов ИИ на организацию.

Считаем токены за текущие сутки (UTC) по AiUsageLog и не пускаем новые ИИ-запросы,
если лимит исчерпан. Дешёвая защита от «дорогой» лавины при массовой сдаче:
и ручной эндпоинт, и deadline-checker спрашивают can_spend() перед вызовом модели.

Лимит настраивается переменной окружения AI_DAILY_TOKEN_BUDGET (по умолчанию
2_000_000 токенов на org в сутки; 0 или отрицательное — бюджет отключён).
"""
import os
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from utils.time import utcnow
from models import AiUsageLog

logger = logging.getLogger(__name__)


def _daily_budget() -> int:
    try:
        return int(os.getenv("AI_DAILY_TOKEN_BUDGET", "2000000"))
    except ValueError:
        return 2_000_000


def tokens_used_today(db: Session, org_type: str) -> int:
    from datetime import datetime, timezone

    now = utcnow()
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).replace(tzinfo=None)
    total = (
        db.query(func.coalesce(func.sum(AiUsageLog.total_tokens), 0))
        .filter(AiUsageLog.org_type == org_type, AiUsageLog.created_at >= start)
        .scalar()
    )
    return int(total or 0)


def can_spend(db: Session, org_type: str) -> bool:
    """False — дневной бюджет токенов организации исчерпан."""
    budget = _daily_budget()
    if budget <= 0:
        return True
    try:
        return tokens_used_today(db, org_type) < budget
    except Exception as e:
        # Сбой подсчёта не должен блокировать проверку работ — логируем и пускаем.
        logger.warning("ai_budget: не удалось посчитать расход токенов (%s)", e)
        return True
