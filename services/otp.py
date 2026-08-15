"""Генерация и проверка одноразовых 6-значных кодов (email verify / reset).

Защита от перебора: код живёт 10 минут, не более 5 попыток ввода на код, при
запросе нового кода старый затирается. Хранится только хэш кода."""
import secrets
from datetime import timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

import models
from security import hash_password, verify_password
from utils.time import utcnow

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
CODE_LENGTH = 6


def _generate_code() -> str:
    # Равномерный 6-значный код с ведущими нулями (000000–999999).
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def issue_code(db: Session, email: str, org_type: str, purpose: str) -> str:
    """Создаёт новый код для (email, org_type, purpose), затирая предыдущий.
    Возвращает код в открытом виде (для отправки письмом)."""
    db.execute(
        delete(models.EmailCode).where(
            models.EmailCode.email == email,
            models.EmailCode.org_type == org_type,
            models.EmailCode.purpose == purpose,
        )
    )
    code = _generate_code()
    row = models.EmailCode(
        email=email,
        org_type=org_type,
        purpose=purpose,
        code_hash=hash_password(code),
        attempts=0,
        expires_at=utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    return code


def verify_code(db: Session, email: str, org_type: str, purpose: str, code: str) -> bool:
    """Проверяет код. При успехе удаляет запись (одноразовость). При ошибке
    инкрементирует счётчик попыток; после MAX_ATTEMPTS код становится мёртвым."""
    # Берём самый свежий код, а не scalar_one_or_none(): уникального индекса на
    # (email, org_type, purpose) в схеме нет, и две параллельные выдачи кода
    # (двойной тап по «Отправить код» — оба запроса проходят delete до вставок)
    # оставляют ДВЕ строки. scalar_one_or_none() в этом случае кидал
    # MultipleResultsFound → 500 на подтверждении, и пользователь не мог
    # войти, пока обе записи не протухнут. Актуален последний выданный код —
    # именно он в письме.
    row = db.execute(
        select(models.EmailCode)
        .where(
            models.EmailCode.email == email,
            models.EmailCode.org_type == org_type,
            models.EmailCode.purpose == purpose,
        )
        .order_by(models.EmailCode.created_at.desc(), models.EmailCode.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if row is None:
        return False
    if row.expires_at < utcnow() or row.attempts >= MAX_ATTEMPTS:
        db.delete(row)
        db.commit()
        return False

    if verify_password(code, row.code_hash):
        db.delete(row)
        db.commit()
        return True

    row.attempts += 1
    db.commit()
    return False
