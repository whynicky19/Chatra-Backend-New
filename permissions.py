"""Общие проверки прав доступа к объектам (защита от IDOR).

Единое место для SQL-проверок членства в чатах и классах —
роутеры не должны дублировать эти запросы.
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Class, Assignment, AssignmentVariant, class_members
from crud import cohorts as crud_cohorts


def require_class_owner(db: Session, class_id: int, current_user) -> Class:
    """Мутации класса/заданий/оценок: только владелец класса или админ.
    Org-проверки недостаточно — иначе любой преподаватель организации правит
    и удаляет ЧУЖИЕ классы (см. SEC-2). Возвращает класс."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls or cls.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    if current_user.role == "admin" or cls.created_by == current_user.id:
        return cls
    raise HTTPException(
        status_code=403,
        detail="Только владелец класса или администратор может это изменить",
    )


def require_assignment_owner(db: Session, assignment, current_user) -> Class:
    """Мутации задания/варианта/оценки — по владению классом задания."""
    return require_class_owner(db, assignment.class_id, current_user)


def require_variant_of_owned_assignment(
    db: Session, assignment_id: int, variant_id: int, current_user
) -> AssignmentVariant:
    """SEC-3: удаление/правка варианта. Вариант должен принадлежать именно
    этому заданию, а задание — владельцу (или админу). Иначе любой
    преподаватель удалит любой вариант по одному variant_id (IDOR)."""
    variant = (
        db.query(AssignmentVariant)
        .filter(AssignmentVariant.id == variant_id)
        .first()
    )
    if not variant or variant.assignment_id != assignment_id:
        raise HTTPException(status_code=404, detail="Variant not found")
    assignment = (
        db.query(Assignment).filter(Assignment.id == assignment_id).first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    require_assignment_owner(db, assignment, current_user)
    return variant


def require_submission_class_owner(db: Session, submission, current_user):
    """Мутации сдачи преподавателем (оценка/статус) — только владелец класса."""
    assignment = (
        db.query(Assignment)
        .filter(Assignment.id == submission.assignment_id)
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return require_assignment_owner(db, assignment, current_user)


def is_class_member(db: Session, class_id: int, user_id: int) -> bool:
    row = db.execute(
        class_members.select().where(
            class_members.c.class_id == class_id,
            class_members.c.user_id == user_id,
        )
    ).fetchone()
    return row is not None


def require_class_access(db: Session, class_id: int, current_user) -> None:
    """Доступ на ЧТЕНИЕ: студент должен состоять в любом потоке класса
    (архивный поток = read-only, но виден). Фолбэк на легаси class_members —
    для классов/баз, где потоки ещё не созданы. Teacher/admin проходят
    по org-проверке роутера."""
    if current_user.role in ("teacher", "admin"):
        return
    if crud_cohorts.is_member_of_any_cohort(db, class_id, current_user.id):
        return
    if not is_class_member(db, class_id, current_user.id):
        raise HTTPException(status_code=403, detail="Вы не состоите в этом классе")


def require_active_cohort_access(db: Session, class_id: int, current_user) -> None:
    """Доступ на ЗАПИСЬ (сдача работ): нужно членство в АКТИВНОМ потоке.
    Ученик архивного потока получает 403 с понятным сообщением.
    Преподаватель-владелец класса и админ имеют доступ всегда.
    Легаси class_id без записи в classes — старое поведение (без потоков)."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if cls is None:
        return  # легаси-задание, потоков нет — работает assignments.deadline
    if current_user.role == "admin" or cls.created_by == current_user.id:
        return
    cohort = crud_cohorts.get_user_cohort_for_class(db, class_id, current_user.id)
    if cohort is not None and cohort.status == "active":
        return
    if cohort is not None:
        raise HTTPException(
            status_code=403,
            detail="Класс доступен только для чтения: ваш учебный год в архиве",
        )
    # Не в потоках, но в легаси class_members и активного потока у класса нет
    # вовсе — база без бэкфилла, ведём себя по-старому.
    if crud_cohorts.get_active_cohort(db, class_id) is None and is_class_member(
        db, class_id, current_user.id
    ):
        return
    raise HTTPException(status_code=403, detail="Вы не состоите в этом классе")
