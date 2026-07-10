from typing import Optional, List
from sqlalchemy.orm import Session
from models import (
    Class, User, AssignmentVariant, Assignment, Submission, Grade, Deadline,
    Cohort, class_members, cohort_students,
)
from sqlalchemy import func
from services.invite_codes import generate_unique_code
from crud import cohorts as crud_cohorts

def create_class(db: Session, name: str, description: Optional[str], created_by: int,
                 org_type: str = "university",
                 cover_image: Optional[str] = None, teacher: Optional[str] = None,
                 period: Optional[str] = None) -> Class:
    obj = Class(
        name=name, description=description, created_by=created_by, org_type=org_type,
        cover_image=cover_image, teacher=teacher, period=period,
        invite_code=generate_unique_code(db),
    )
    db.add(obj)
    db.flush()
    # Сразу создаём активный поток текущего учебного года — без него в класс
    # нельзя вступить по коду.
    crud_cohorts.create_initial_cohort(db, obj.id)
    db.commit()
    db.refresh(obj)
    return obj

def get_class(db: Session, class_id: int) -> Optional[Class]:
    return db.query(Class).filter(Class.id == class_id).first()

def get_class_by_invite_code(db: Session, code: str) -> Optional[Class]:
    return db.query(Class).filter(Class.invite_code == code).first()

def regenerate_invite_code(db: Session, class_id: int) -> Optional[Class]:
    obj = get_class(db, class_id)
    if not obj:
        return None
    obj.invite_code = generate_unique_code(db)
    db.commit()
    db.refresh(obj)
    return obj

def get_all_classes(db: Session, teacher_id: Optional[int] = None,
                    org_type: Optional[str] = None) -> List[Class]:
    q = db.query(Class)
    if org_type is not None:
        q = q.filter(Class.org_type == org_type)
    if teacher_id is not None:
        q = q.filter(Class.created_by == teacher_id)
    return q.order_by(Class.created_at.desc()).all()

def update_class(db: Session, class_id: int, data: dict) -> Optional[Class]:
    obj = get_class(db, class_id)
    if not obj:
        return None
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj

def delete_class(db: Session, class_id: int) -> bool:
    obj = get_class(db, class_id)
    if not obj:
        return False
    # Удаление класса каскадит на потоки → дедлайны, но submissions.deadline_id
    # ссылается на дедлайны без ON DELETE (FK submissions_deadline_id_fkey).
    # Обнуляем ссылки заранее, иначе ForeignKeyViolation. Сами сдачи — история
    # ученика, их не трогаем (для них останется fallback assignment.deadline).
    deadline_ids = (
        db.query(Deadline.id)
        .join(Cohort, Cohort.id == Deadline.cohort_id)
        .filter(Cohort.class_id == class_id)
        .subquery()
    )
    db.query(Submission).filter(Submission.deadline_id.in_(deadline_ids)).update(
        {Submission.deadline_id: None}, synchronize_session=False
    )
    db.delete(obj)
    db.commit()
    return True

def add_member(db: Session, class_id: int, user_id: int) -> bool:
    """Вступление = членство в АКТИВНОМ потоке. class_members продолжает
    наполняться (двойная запись): её читают permissions.py и легаси-скрипты;
    уберём отдельной задачей после переезда."""
    obj = get_class(db, class_id)
    if not obj:
        return False
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    cohort = crud_cohorts.get_active_cohort(db, class_id)
    if cohort:
        crud_cohorts.add_student_to_cohort(db, cohort.id, user_id)
    if user not in obj.members:
        obj.members.append(user)
    db.commit()
    return True

def remove_member(db: Session, class_id: int, user_id: int) -> bool:
    """Убирает из активного потока и из легаси class_members. Архивные
    членства не трогаем — это история прошлых учебных лет."""
    obj = get_class(db, class_id)
    if not obj:
        return False
    cohort = crud_cohorts.get_active_cohort(db, class_id)
    if cohort:
        crud_cohorts.remove_student_from_cohort(db, cohort.id, user_id)
    user = db.query(User).filter(User.id == user_id).first()
    if user and user in obj.members:
        obj.members.remove(user)
    db.commit()
    return True

def get_members(db: Session, class_id: int, cohort_id: Optional[int] = None) -> List[User]:
    """Ученики потока: по умолчанию активного, cohort_id — конкретного
    (просмотр архива преподавателем). Класс без потоков — легаси class_members."""
    obj = get_class(db, class_id)
    if not obj:
        return []
    cohort = (
        crud_cohorts.get_cohort(db, cohort_id)
        if cohort_id is not None
        else crud_cohorts.get_active_cohort(db, class_id)
    )
    if cohort is None:
        return obj.members
    return (
        db.query(User)
        .join(cohort_students, cohort_students.c.student_id == User.id)
        .filter(cohort_students.c.cohort_id == cohort.id)
        .all()
    )

def add_variant(
    db: Session,
    assignment_id: int,
    variant_number: int,
    reference_solution_url: str,
    title: Optional[str] = None,
) -> AssignmentVariant:
    existing = db.query(AssignmentVariant).filter(
        AssignmentVariant.assignment_id == assignment_id,
        AssignmentVariant.variant_number == variant_number,
    ).first()
    if existing:
        db.delete(existing)
        db.flush()

    obj = AssignmentVariant(
        assignment_id=assignment_id,
        variant_number=variant_number,
        title=title or f"Вариант {variant_number}",
        reference_solution_url=reference_solution_url,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj

def get_variants(db: Session, assignment_id: int) -> List[AssignmentVariant]:
    return (
        db.query(AssignmentVariant)
        .filter(AssignmentVariant.assignment_id == assignment_id)
        .order_by(AssignmentVariant.variant_number)
        .all()
    )

def delete_variant(db: Session, variant_id: int) -> bool:
    obj = db.query(AssignmentVariant).filter(AssignmentVariant.id == variant_id).first()
    if not obj:
        return False
    db.delete(obj)
    db.commit()
    return True

def get_variant_by_number(db: Session, assignment_id: int, variant_number: int) -> Optional[AssignmentVariant]:
    return db.query(AssignmentVariant).filter(
        AssignmentVariant.assignment_id == assignment_id,
        AssignmentVariant.variant_number == variant_number,
    ).first()

def get_student_rating(db: Session, class_id: Optional[int] = None,
                       org_type: Optional[str] = None,
                       cohort_id: Optional[int] = None) -> list:
    q = (
        db.query(
            User.id.label("student_id"),
            User.full_name.label("full_name"),
            func.sum(Grade.score).label("total_score"),
            func.count(Grade.id).label("graded_count"),
            func.avg(Grade.score).label("avg_score"),
        )
        .join(Submission, Submission.student_id == User.id)
        .join(Grade, Grade.submission_id == Submission.id)
        .filter(User.role == "student")
    )

    if org_type is not None:
        q = q.filter(User.org_type == org_type)

    if class_id is not None:
        q = q.filter(
            Submission.assignment_id.in_(
                db.query(Assignment.id).filter(Assignment.class_id == class_id)
            )
        )
        # Рейтинг класса считается в рамках потока: по умолчанию активного,
        # cohort_id — конкретного (архивы для преподавателя). Класс без
        # потоков — легаси-поведение (весь класс).
        cohort = (
            crud_cohorts.get_cohort(db, cohort_id)
            if cohort_id is not None
            else crud_cohorts.get_active_cohort(db, class_id)
        )
        if cohort is not None:
            q = q.filter(
                User.id.in_(
                    db.query(cohort_students.c.student_id).filter(
                        cohort_students.c.cohort_id == cohort.id
                    )
                ),
                # Сдачи чужих потоков (например, прошлый год того же ученика)
                # не попадают в рейтинг; NULL — сдачи заданий без дедлайна.
                (Submission.deadline_id.is_(None))
                | Submission.deadline_id.in_(
                    db.query(Deadline.id).filter(Deadline.cohort_id == cohort.id)
                ),
            )

    rows = q.group_by(User.id, User.full_name).order_by(func.sum(Grade.score).desc()).all()

    # SEC-4: e-mail (PII) из рейтинга исключён. Отдаём отображаемое имя;
    # если ФИО не задано — обезличенный "Студент #<id>", а не логин-почту.
    return [
        {
            "student_id": r.student_id,
            "full_name": (r.full_name or "").strip() or f"Студент #{r.student_id}",
            "total_score": int(r.total_score or 0),
            "graded_count": int(r.graded_count or 0),
            "avg_score": round(float(r.avg_score or 0), 1),
        }
        for r in rows
    ]
