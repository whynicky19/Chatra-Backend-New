"""Потоки (когорты): членство по учебным годам и дедлайны по потокам.

Единая точка чтения дедлайна — resolve_deadline / deadlines_map. Порядок
везде один: запись deadlines по потоку пользователя, если записи нет —
fallback на deprecated-поле assignments.deadline. Неопубликованная запись
(черновик после rollover) СКРЫВАЕТ задание от студентов потока целиком —
см. is_hidden_for_user; deadline=None остаётся только у заданий, у которых
дедлайна не было изначально.
"""
from datetime import date, datetime
from utils.time import utcnow
from typing import Dict, List, Optional, Tuple

from sqlalchemy import case
from sqlalchemy.orm import Session

from models import Assignment, Class, Cohort, Deadline, User, cohort_students


def current_academic_year(today: Optional[date] = None) -> str:
    """«2026/2027» — учебный год начинается 1 сентября."""
    today = today or date.today()
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}/{start + 1}"


def default_start_date(today: Optional[date] = None) -> date:
    today = today or date.today()
    start = today.year if today.month >= 9 else today.year - 1
    return date(start, 9, 1)


def get_cohort(db: Session, cohort_id: int) -> Optional[Cohort]:
    return db.query(Cohort).filter(Cohort.id == cohort_id).first()


def get_active_cohort(db: Session, class_id: int) -> Optional[Cohort]:
    return (
        db.query(Cohort)
        .filter(Cohort.class_id == class_id, Cohort.status == "active")
        .first()
    )


def get_cohorts_for_class(db: Session, class_id: int) -> List[Cohort]:
    return (
        db.query(Cohort)
        .filter(Cohort.class_id == class_id)
        .order_by(Cohort.start_date.desc())
        .all()
    )


def create_initial_cohort(db: Session, class_id: int) -> Cohort:
    """Первый активный поток нового класса — без него нельзя вступить по коду."""
    cohort = Cohort(
        class_id=class_id,
        academic_year=current_academic_year(),
        start_date=default_start_date(),
        status="active",
    )
    db.add(cohort)
    db.flush()
    return cohort


def is_cohort_member(db: Session, cohort_id: int, user_id: int) -> bool:
    row = db.execute(
        cohort_students.select().where(
            cohort_students.c.cohort_id == cohort_id,
            cohort_students.c.student_id == user_id,
        )
    ).fetchone()
    return row is not None


def add_student_to_cohort(db: Session, cohort_id: int, user_id: int) -> None:
    """Идемпотентно; commit на вызывающей стороне."""
    if not is_cohort_member(db, cohort_id, user_id):
        db.execute(
            cohort_students.insert().values(
                cohort_id=cohort_id, student_id=user_id, joined_at=utcnow()
            )
        )


def remove_student_from_cohort(db: Session, cohort_id: int, user_id: int) -> None:
    db.execute(
        cohort_students.delete().where(
            cohort_students.c.cohort_id == cohort_id,
            cohort_students.c.student_id == user_id,
        )
    )


def get_user_cohort_for_class(db: Session, class_id: int, user_id: int) -> Optional[Cohort]:
    """Поток пользователя в классе: активный приоритетнее, затем самый свежий."""
    return (
        db.query(Cohort)
        .join(cohort_students, cohort_students.c.cohort_id == Cohort.id)
        .filter(
            Cohort.class_id == class_id,
            cohort_students.c.student_id == user_id,
        )
        .order_by(case((Cohort.status == "active", 0), else_=1), Cohort.start_date.desc())
        .first()
    )


def is_member_of_any_cohort(db: Session, class_id: int, user_id: int) -> bool:
    return get_user_cohort_for_class(db, class_id, user_id) is not None


def is_member_of_active_cohort(db: Session, class_id: int, user_id: int) -> bool:
    cohort = get_user_cohort_for_class(db, class_id, user_id)
    return cohort is not None and cohort.status == "active"


def is_archived_for_user(db: Session, class_id: int, user_id: int) -> bool:
    """True, если пользователь состоит в потоках класса, но только в архивных."""
    cohort = get_user_cohort_for_class(db, class_id, user_id)
    return cohort is not None and cohort.status != "active"


def get_cohort_student_ids(db: Session, cohort_id: int) -> List[int]:
    rows = db.execute(
        cohort_students.select().where(cohort_students.c.cohort_id == cohort_id)
    ).fetchall()
    return [r.student_id for r in rows]


def _cohort_for_deadlines(db: Session, class_id: int, user: User) -> Optional[Cohort]:
    """Чей поток определяет дедлайны: у студента — его собственный,
    у преподавателя/админа — активный поток класса (черновики видны)."""
    if user.role in ("teacher", "admin"):
        return get_active_cohort(db, class_id)
    return get_user_cohort_for_class(db, class_id, user.id)


def resolve_deadline(
    db: Session, assignment: Assignment, user: User
) -> Tuple[Optional[Deadline], Optional[datetime]]:
    """Единая точка чтения дедлайна одного задания.

    Возвращает (строка deadlines или None, действующая дата или None).
    Правила: есть запись по потоку -> её дата (студенту неопубликованная
    запись даёт None-дату — но такое задание для него целиком скрыто,
    см. is_hidden_for_user); записи нет -> fallback на deprecated
    assignments.deadline.
    """
    cohort = _cohort_for_deadlines(db, assignment.class_id, user)
    if cohort is None:
        # Легаси class_id (нет класса/потока) — старое поведение.
        return None, assignment.deadline

    row = (
        db.query(Deadline)
        .filter(Deadline.cohort_id == cohort.id, Deadline.assignment_id == assignment.id)
        .first()
    )
    if row is None:
        return None, assignment.deadline
    if user.role == "student" and not row.is_published:
        return row, None
    return row, row.due_date


def is_hidden_for_user(db: Session, assignment: Assignment, user: User) -> bool:
    """Черновик дедлайна (is_published=False) в потоке студента скрывает
    задание целиком — до публикации его как будто не существует.
    Преподаватель/админ видят всё."""
    if user.role in ("teacher", "admin"):
        return False
    row, _ = resolve_deadline(db, assignment, user)
    return row is not None and not row.is_published


def deadlines_map(
    db: Session, assignments: List[Assignment], user: User
) -> Tuple[Dict[int, Optional[datetime]], set]:
    """Bulk-версия resolve_deadline/is_hidden_for_user для списков (без N+1).
    Возвращает (id задания -> действующая дата, id заданий, скрытых от
    пользователя черновиками)."""
    result: Dict[int, Optional[datetime]] = {a.id: a.deadline for a in assignments}
    hidden: set = set()
    if not assignments:
        return result, hidden

    class_ids = {a.class_id for a in assignments}
    cohort_by_class: Dict[int, int] = {}
    for class_id in class_ids:
        cohort = _cohort_for_deadlines(db, class_id, user)
        if cohort:
            cohort_by_class[class_id] = cohort.id
    if not cohort_by_class:
        return result, hidden

    assignment_by_id = {a.id: a for a in assignments}
    rows = (
        db.query(Deadline)
        .filter(
            Deadline.cohort_id.in_(set(cohort_by_class.values())),
            Deadline.assignment_id.in_(assignment_by_id.keys()),
        )
        .all()
    )
    for row in rows:
        a = assignment_by_id.get(row.assignment_id)
        # Строка релевантна, только если это поток класса самого задания.
        if not a or cohort_by_class.get(a.class_id) != row.cohort_id:
            continue
        if user.role == "student" and not row.is_published:
            hidden.add(a.id)
        else:
            result[a.id] = row.due_date
    return result, hidden


def rollover_class(db: Session, cls: Class, new_academic_year: str, new_start_date: date) -> dict:
    """Переход класса на новый учебный год. Одна транзакция на класс:
    активный поток архивируется (строка берётся с блокировкой FOR UPDATE —
    двойной клик по кнопке не создаст два потока; в SQLite блокировку
    подстраховывает partial unique index), создаётся новый активный поток,
    дедлайны копируются со сдвигом на разницу start_date черновиками
    (is_published=False). Ученики НЕ переносятся. Commit/rollback на
    вызывающей стороне."""
    active = (
        db.query(Cohort)
        .filter(Cohort.class_id == cls.id, Cohort.status == "active")
        .with_for_update()
        .first()
    )
    if active is None:
        return {"class_id": cls.id, "status": "no_active_cohort"}
    if active.academic_year == new_academic_year:
        # Повторный вызов для уже перенесённого класса — не ломаем данные.
        return {"class_id": cls.id, "status": "already_rolled", "new_cohort_id": active.id}

    active.status = "archived"
    new_cohort = Cohort(
        class_id=cls.id,
        academic_year=new_academic_year,
        start_date=new_start_date,
        status="active",
    )
    db.add(new_cohort)
    db.flush()

    shift = new_start_date - active.start_date
    old_rows = {
        d.assignment_id: d
        for d in db.query(Deadline).filter(Deadline.cohort_id == active.id).all()
    }
    created = 0
    assignments = db.query(Assignment).filter(Assignment.class_id == cls.id).all()
    for a in assignments:
        old = old_rows.get(a.id)
        base = old.due_date if old else a.deadline  # тот же fallback, что и при чтении
        if base is None:
            continue
        upsert_deadline(db, new_cohort.id, a.id, base + shift, is_published=False)
        created += 1

    return {
        "class_id": cls.id,
        "status": "rolled",
        "new_cohort_id": new_cohort.id,
        "deadlines_created": created,
    }


def upsert_deadline(
    db: Session,
    cohort_id: int,
    assignment_id: int,
    due_date: datetime,
    is_published: bool = True,
) -> Deadline:
    """Создаёт/обновляет дедлайн задания в потоке; commit на вызывающей стороне."""
    row = (
        db.query(Deadline)
        .filter(Deadline.cohort_id == cohort_id, Deadline.assignment_id == assignment_id)
        .first()
    )
    if row:
        row.due_date = due_date
        row.is_published = is_published
        return row
    row = Deadline(
        cohort_id=cohort_id,
        assignment_id=assignment_id,
        due_date=due_date,
        is_published=is_published,
    )
    db.add(row)
    db.flush()
    return row
