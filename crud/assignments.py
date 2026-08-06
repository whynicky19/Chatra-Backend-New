import json
from datetime import datetime
from utils.time import utcnow
from typing import Optional, List

from sqlalchemy import or_
from sqlalchemy.orm import Session
from models import Assignment, Submission, Grade, User, Cohort, Deadline, cohort_students
from services.file_cleanup import delete_assignment_files, delete_upload_file

def create_assignment(
    db: Session,
    class_id: int,
    title: str,
    description: Optional[str],
    criteria: list,
    max_score: int,
    deadline: Optional[datetime],
    created_by: int,
    reference_solution_url: Optional[str] = None,
) -> Assignment:
    obj = Assignment(
        class_id=class_id,
        title=title,
        description=description,
        criteria=json.dumps(criteria, ensure_ascii=False),
        max_score=max_score,
        deadline=deadline,
        created_by=created_by,
        reference_solution_url=reference_solution_url,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj

def get_assignment(db: Session, assignment_id: int) -> Optional[Assignment]:
    return db.query(Assignment).filter(Assignment.id == assignment_id).first()


def _parse_reference_urls(raw: Optional[str]) -> List[str]:
    """Поле reference_solution_url хранит либо один URL, либо JSON-массив
    URL (несколько файлов эталона)."""
    if not raw:
        return []
    if raw.startswith('['):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [u for u in parsed if u]
        except Exception:
            pass
    return [raw]


def resolve_reference_solution_urls(
    db: Session, assignment: Assignment, submission: Submission,
) -> List[str]:
    """Эталонные файлы для проверки ИИ конкретной сдачи (общий эталон задания)."""
    return _parse_reference_urls(assignment.reference_solution_url)

def get_all_assignments(db: Session, class_id: Optional[int] = None, active_only: bool = False,
                        org_type: Optional[str] = None,
                        limit: Optional[int] = None, offset: int = 0,
                        exclude_author_ids=None) -> List[Assignment]:
    q = db.query(Assignment)
    if class_id is not None:
        q = q.filter(Assignment.class_id == class_id)
    # Модерация UGC: задания авторов из блок-листа не отдаём (серверная
    # фильтрация — см. services/moderation.py).
    if exclude_author_ids:
        q = q.filter(~Assignment.created_by.in_(list(exclude_author_ids)))
    if org_type is not None:
        # Организация задания определяется по его создателю: у части заданий
        # class_id исторически ссылается на легаси-посты, поэтому фильтровать
        # через таблицу classes нельзя.
        q = q.join(User, User.id == Assignment.created_by).filter(User.org_type == org_type)
    if active_only:
        q = q.filter(Assignment.is_active == True)
    q = q.order_by(Assignment.created_at.desc())
    # FE-1: пагинация. limit=None — вернуть всё (фоновые задачи вроде
    # deadline_checker, которым нужен полный список).
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()

def update_assignment(db: Session, assignment_id: int, data: dict) -> Optional[Assignment]:
    obj = get_assignment(db, assignment_id)
    if not obj:
        return None
    if "criteria" in data and isinstance(data["criteria"], list):
        data["criteria"] = json.dumps(data["criteria"], ensure_ascii=False)
    old_reference_solution_url = obj.reference_solution_url
    for key, value in data.items():
        if value is not None:
            setattr(obj, key, value)
    db.commit()
    db.refresh(obj)
    # BE-10: референсное решение заменили — старый файл больше не нужен.
    if (
        "reference_solution_url" in data
        and old_reference_solution_url
        and old_reference_solution_url != obj.reference_solution_url
    ):
        delete_upload_file(old_reference_solution_url)
    return obj

def delete_assignment(db: Session, assignment_id: int) -> bool:
    obj = get_assignment(db, assignment_id)
    if not obj:
        return False
    # BE-10: варианты и сдачи уходят каскадом вместе с заданием (ORM
    # cascade="all, delete-orphan") — их файлы чистим до удаления записи.
    delete_assignment_files(obj)
    db.delete(obj)
    db.commit()
    return True

def get_submission(db: Session, submission_id: int) -> Optional[Submission]:
    return db.query(Submission).filter(Submission.id == submission_id).first()

def get_submissions_for_assignment(db: Session, assignment_id: int,
                                   cohort: Optional[Cohort] = None,
                                   limit: Optional[int] = None, offset: int = 0,
                                   exclude_author_ids=None) -> List[Submission]:
    """Сдачи задания; с cohort — только его учебного года: сдачи учеников
    потока плюс сдачи, заякоренные на дедлайн потока (ученик мог быть
    исключён из потока позже)."""
    q = db.query(Submission).filter(Submission.assignment_id == assignment_id)
    if cohort is not None:
        q = q.filter(
            or_(
                Submission.student_id.in_(
                    db.query(cohort_students.c.student_id).filter(
                        cohort_students.c.cohort_id == cohort.id
                    )
                ),
                Submission.deadline_id.in_(
                    db.query(Deadline.id).filter(Deadline.cohort_id == cohort.id)
                ),
            )
        )
    # Модерация UGC: сдачи заблокированных учеников скрываем от учителя.
    if exclude_author_ids:
        q = q.filter(~Submission.student_id.in_(list(exclude_author_ids)))
    q = q.order_by(Submission.submitted_at.desc())
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()

def get_submissions_for_student(db: Session, student_id: int,
                                limit: Optional[int] = None, offset: int = 0) -> List[Submission]:
    q = (
        db.query(Submission)
        .filter(Submission.student_id == student_id)
        .order_by(Submission.submitted_at.desc())
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()

def student_already_submitted(db: Session, assignment_id: int, student_id: int) -> bool:
    return (
        db.query(Submission)
        .filter(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student_id,
        )
        .first()
        is not None
    )

def set_submission_status(db: Session, submission_id: int, status: str) -> Optional[Submission]:
    obj = get_submission(db, submission_id)
    if not obj:
        return None
    obj.status = status
    db.commit()
    db.refresh(obj)
    return obj

def set_submission_ai_confidence(
    db: Session, submission_id: int, confidence: Optional[int], reasons: Optional[list]
) -> Optional[Submission]:
    obj = get_submission(db, submission_id)
    if not obj:
        return None
    obj.ai_confidence = confidence
    obj.ai_review_reasons = json.dumps(reasons or [], ensure_ascii=False)
    db.commit()
    db.refresh(obj)
    return obj

def create_or_update_grade(
    db: Session,
    submission_id: int,
    score: int,
    feedback: Optional[str],
    criteria_scores: Optional[list],
    graded_by: str = "ai",
) -> Grade:
    existing = db.query(Grade).filter(Grade.submission_id == submission_id).first()
    criteria_json = json.dumps(criteria_scores, ensure_ascii=False) if criteria_scores else None

    if existing:
        existing.score = score
        existing.feedback = feedback
        existing.criteria_scores = criteria_json
        existing.graded_by = graded_by
        existing.graded_at = utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    grade = Grade(
        submission_id=submission_id,
        score=score,
        feedback=feedback,
        criteria_scores=criteria_json,
        graded_by=graded_by,
    )
    db.add(grade)
    db.commit()
    db.refresh(grade)
    return grade

def get_grade_by_submission(db: Session, submission_id: int) -> Optional[Grade]:
    return db.query(Grade).filter(Grade.submission_id == submission_id).first()
