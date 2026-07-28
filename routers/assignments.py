import json as _json
from utils.time import utcnow
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import schemas
from crud import assignments as crud
from crud import classes as crud_classes
from crud import cohorts as crud_cohorts
from db import get_db
from deps import get_current_user, get_current_teacher
from models import Class as ClassModel
from permissions import (
    require_class_access,
    require_active_cohort_access,
    require_assignment_owner,
    require_variant_of_owned_assignment,
    require_submission_class_owner,
)
from services.ai_grader import grade_submission as _ai_grade
from services.ai_grader import grade_handwritten_submission as _ai_grade_handwritten
from services.ai_grader import AI_CONFIDENCE_THRESHOLD
from routers.ai import _check_rate_limit

router = APIRouter(tags=["Assignments"])

_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "gif"}


def _check_assignment_org(db: Session, assignment, current_user):

    cls = db.query(ClassModel).filter(ClassModel.id == assignment.class_id).first()
    if cls and cls.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Assignment not found")


def _check_assignment_access(db: Session, assignment, current_user):
    """Org-проверка + членство студента в классе. Задание с неопубликованным
    дедлайном (черновик после rollover) для студента скрыто — 404."""
    _check_assignment_org(db, assignment, current_user)
    require_class_access(db, assignment.class_id, current_user)
    if crud_cohorts.is_hidden_for_user(db, assignment, current_user):
        raise HTTPException(status_code=404, detail="Assignment not found")


def _with_cohort_deadline(assignment, due_date) -> schemas.AssignmentResponseFull:
    """Ответ задания с датой дедлайна из потока пользователя (единая логика
    в crud_cohorts.resolve_deadline/deadlines_map). ORM-объект не мутируем —
    случайный commit записал бы чужую дату в deprecated-поле."""
    resp = schemas.AssignmentResponseFull.model_validate(assignment)
    resp.deadline = due_date
    return resp


def _check_submission_org(db: Session, submission, current_user):

    from models import Assignment
    assignment = db.query(Assignment).filter(Assignment.id == submission.assignment_id).first()
    if assignment:
        cls = db.query(ClassModel).filter(ClassModel.id == assignment.class_id).first()
        if cls and cls.org_type != current_user.org_type:
            raise HTTPException(status_code=404, detail="Submission not found")




def _notify_grade(submission, assignment, score: int, max_score: int) -> None:
    """Push студенту о выставленной оценке. Никогда не роняет запрос."""
    try:
        from services.fcm import send_push_bg

        title = "Работа оценена"
        body = f"«{assignment.title}» — {score}/{max_score}"
        send_push_bg(
            [submission.student_id],
            title,
            body,
            {
                "type": "grade",
                "notif_key": f"grade:{submission.id}",
                "submission_id": submission.id,
                "assignment_id": assignment.id,
                "class_id": assignment.class_id,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _student_submission_view(sub) -> "schemas.SubmissionWithGrade":
    """Ответ сдачи для студента: confidence/причины самопроверки ИИ не
    показываем никогда; при needs_review дополнительно скрываем саму оценку
    (Grade.graded_by == "ai_suggested" — предложение ИИ, не финальная оценка,
    её видит только учитель до подтверждения)."""
    resp = schemas.SubmissionWithGrade.model_validate(sub)
    updates = {"ai_confidence": None, "ai_review_reasons": None}
    if sub.status == "needs_review":
        updates["grade"] = None
    return resp.model_copy(update=updates)


def _notify_needs_review(submission, assignment) -> None:
    """Push студенту: работа передана на ручную проверку. Без чисел/деталей
    распознавания — студент не должен видеть уверенность ИИ."""
    try:
        from services.fcm import send_push_bg

        title = "Работа на проверке"
        body = f"«{assignment.title}» — проверяется учителем"
        send_push_bg(
            [submission.student_id],
            title,
            body,
            {
                "type": "grade",
                "notif_key": f"grade:{submission.id}",
                "submission_id": submission.id,
                "assignment_id": assignment.id,
                "class_id": assignment.class_id,
            },
        )
    except Exception:  # noqa: BLE001
        pass


@router.post(
    "/assignments/",
    response_model=schemas.AssignmentResponseFull,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment(
    body: schemas.AssignmentCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    criteria_list = [c.model_dump() for c in body.criteria]
    obj = crud.create_assignment(
        db=db,
        class_id=body.class_id,
        title=body.title,
        description=body.description,
        criteria=criteria_list,
        max_score=body.max_score,
        deadline=body.deadline,
        created_by=current_user.id,
        reference_solution_url=body.reference_solution_url,
    )
    # Дедлайн дублируется в активный поток класса — источник правды теперь
    # таблица deadlines, поле задания остаётся для обратной совместимости.
    if body.deadline:
        cohort = crud_cohorts.get_active_cohort(db, body.class_id)
        if cohort:
            crud_cohorts.upsert_deadline(db, cohort.id, obj.id, body.deadline, is_published=True)
            db.commit()
    return obj


@router.get("/assignments/", response_model=List[schemas.AssignmentResponseFull])
def list_assignments(
    class_id: Optional[int] = None,
    active_only: bool = False,
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if class_id is not None:
        cls = db.query(ClassModel).filter(ClassModel.id == class_id).first()
        if cls and cls.org_type != current_user.org_type:
            raise HTTPException(status_code=404, detail="Assignment not found")
        require_class_access(db, class_id, current_user)
    items = crud.get_all_assignments(db, class_id=class_id, active_only=active_only,
                                     org_type=current_user.org_type,
                                     limit=limit, offset=offset)
    # Задания-черновики (неопубликованный дедлайн потока) не показываем ученикам.
    dmap, hidden = crud_cohorts.deadlines_map(db, items, current_user)
    return [
        _with_cohort_deadline(a, dmap.get(a.id, a.deadline))
        for a in items
        if a.id not in hidden
    ]


# NOTE: Must be before /assignments/{assignment_id}
@router.get("/assignments/student/my-submissions", response_model=List[schemas.SubmissionWithGrade])
def my_submissions(
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    subs = crud.get_submissions_for_student(db, current_user.id, limit=limit, offset=offset)
    return [_student_submission_view(s) for s in subs]


@router.get("/assignments/student/my-rating")
def my_rating(
    class_id: int = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    from sqlalchemy import text as sqlt
    try:
        if class_id:
            rows = db.execute(sqlt("""
                SELECT g.score, a.max_score
                FROM grades g
                JOIN submissions s ON s.id = g.submission_id
                JOIN assignments a ON a.id = s.assignment_id
                WHERE s.student_id = :uid AND a.class_id = :cid
            """), {"uid": current_user.id, "cid": class_id}).fetchall()
        else:
            rows = db.execute(sqlt("""
                SELECT g.score, a.max_score
                FROM grades g
                JOIN submissions s ON s.id = g.submission_id
                JOIN assignments a ON a.id = s.assignment_id
                WHERE s.student_id = :uid
            """), {"uid": current_user.id}).fetchall()

        if not rows:
            return {"avg_score": 0, "avg_percent": 0, "graded_count": 0, "total_score": 0, "max_possible": 0}

        total_score = sum(r[0] or 0 for r in rows)
        max_possible = sum(r[1] or 100 for r in rows)
        avg_score = round(total_score / len(rows), 1)
        avg_percent = round((total_score / max_possible * 100) if max_possible > 0 else 0, 1)

        return {
            "avg_score": avg_score,
            "avg_percent": avg_percent,
            "graded_count": len(rows),
            "total_score": total_score,
            "max_possible": max_possible,
        }
    except Exception:
        import logging
        logging.getLogger(__name__).exception("my_rating failed")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


@router.get("/assignments/{assignment_id}", response_model=schemas.AssignmentResponseFull)
def get_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_assignment(db, assignment_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Assignment not found")
    _check_assignment_access(db, obj, current_user)
    _, due_date = crud_cohorts.resolve_deadline(db, obj, current_user)
    return _with_cohort_deadline(obj, due_date)


@router.put("/assignments/{assignment_id}", response_model=schemas.AssignmentResponseFull)
def update_assignment(
    assignment_id: int,
    body: schemas.AssignmentUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_assignment(db, assignment_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Assignment not found")
    require_assignment_owner(db, obj, current_user)  # SEC-2
    data = body.model_dump(exclude_none=True)
    obj = crud.update_assignment(db, assignment_id, data)
    # Правка дедлайна задания = правка дедлайна АКТИВНОГО потока (архивные
    # годы не трогаем). Точечная правка по потокам — PATCH дедлайнов потока.
    if "deadline" in data:
        cohort = crud_cohorts.get_active_cohort(db, obj.class_id)
        if cohort:
            crud_cohorts.upsert_deadline(db, cohort.id, obj.id, data["deadline"], is_published=True)
            db.commit()
    return obj


@router.delete("/assignments/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_assignment(db, assignment_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Assignment not found")
    require_assignment_owner(db, obj, current_user)  # SEC-2
    crud.delete_assignment(db, assignment_id)




@router.get(
    "/assignments/{assignment_id}/variants",
    response_model=List[schemas.VariantResponse],
)
def list_variants(
    assignment_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    obj = crud.get_assignment(db, assignment_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Assignment not found")
    _check_assignment_access(db, obj, current_user)
    return crud_classes.get_variants(db, assignment_id)


@router.post(
    "/assignments/{assignment_id}/variants",
    response_model=schemas.VariantResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_variant(
    assignment_id: int,
    body: schemas.VariantCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_assignment(db, assignment_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Assignment not found")
    require_assignment_owner(db, obj, current_user)  # SEC-2
    return crud_classes.add_variant(
        db=db,
        assignment_id=assignment_id,
        variant_number=body.variant_number,
        reference_solution_url=body.reference_solution_url,
        title=body.title,
    )


@router.delete(
    "/assignments/{assignment_id}/variants/{variant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_variant(
    assignment_id: int,
    variant_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    # SEC-3: вариант должен принадлежать этому заданию владельца — иначе
    # любой преподаватель удалял любой вариант по одному variant_id (IDOR).
    require_variant_of_owned_assignment(db, assignment_id, variant_id, current_user)
    if not crud_classes.delete_variant(db, variant_id):
        raise HTTPException(status_code=404, detail="Variant not found")




@router.post(
    "/assignments/{assignment_id}/submit",
    response_model=schemas.SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_assignment(
    assignment_id: int,
    body: schemas.SubmissionCreateV2,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    assignment = crud.get_assignment(db, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    _check_assignment_access(db, assignment, current_user)
    # Сдавать можно только в активном потоке; архивный ученик получает 403
    # с понятным сообщением (класс для него read-only).
    require_active_cohort_access(db, assignment.class_id, current_user)
    if not assignment.is_active:
        raise HTTPException(status_code=400, detail="Assignment is closed")

    if crud.student_already_submitted(db, assignment_id, current_user.id):
        raise HTTPException(status_code=409, detail="You already submitted this assignment")

    if not body.text_content and not body.file_url and not body.file_urls:
        raise HTTPException(status_code=422, detail="Provide text_content, file_url or file_urls")

    # Validate variant_number if variants exist
    variants = crud_classes.get_variants(db, assignment_id)
    if variants and body.variant_number is None:
        raise HTTPException(
            status_code=422,
            detail=f"Это задание имеет варианты ({len(variants)} шт.). Укажите variant_number.",
        )
    if body.variant_number and variants:
        valid_numbers = [v.variant_number for v in variants]
        if body.variant_number not in valid_numbers:
            raise HTTPException(
                status_code=422,
                detail=f"Вариант {body.variant_number} не найден. Доступные: {valid_numbers}",
            )

    # Просрочка считается по дедлайну потока ученика; сдача якорится к deadline_id.
    deadline_row, due_date = crud_cohorts.resolve_deadline(db, assignment, current_user)
    is_late = bool(due_date and utcnow() > due_date)

    all_file_urls: list = []
    if body.file_url:
        all_file_urls.append(body.file_url)
    if body.file_urls:
        all_file_urls.extend(body.file_urls)

    from models import Submission
    import json
    sub_obj = Submission(
        assignment_id=assignment_id,
        student_id=current_user.id,
        deadline_id=deadline_row.id if deadline_row else None,
        text_content=body.text_content,
        file_url=all_file_urls[0] if all_file_urls else None,
        file_urls=json.dumps(all_file_urls) if all_file_urls else None,
        variant_number=body.variant_number,
        status="late" if is_late else "submitted",
    )
    db.add(sub_obj)
    try:
        db.commit()
    except IntegrityError:
        # BE-1: гонка — параллельная сдача успела вставиться между проверкой
        # student_already_submitted() и этим commit. БД-инвариант отбил дубль.
        db.rollback()
        raise HTTPException(status_code=409, detail="You already submitted this assignment")
    db.refresh(sub_obj)
    return sub_obj


@router.get(
    "/assignments/{assignment_id}/submissions",
    response_model=List[schemas.SubmissionWithGrade],
)
def get_submissions(
    assignment_id: int,
    cohort_id: Optional[int] = None,
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Сдачи активного потока; cohort_id — просмотр прошлых лет.
    Класс без потоков (легаси class_id) — все сдачи, как раньше."""
    assignment = crud.get_assignment(db, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    require_assignment_owner(db, assignment, current_user)  # SEC-2
    if cohort_id is not None:
        cohort = crud_cohorts.get_cohort(db, cohort_id)
        if not cohort or cohort.class_id != assignment.class_id:
            raise HTTPException(status_code=404, detail="Поток не найден")
    else:
        cohort = crud_cohorts.get_active_cohort(db, assignment.class_id)
    subs = crud.get_submissions_for_assignment(db, assignment_id, cohort=cohort,
                                               limit=limit, offset=offset)

    # BE-7: одним запросом тянем всех студентов сдач вместо User-запроса на
    # каждую сдачу (N+1).
    from models import User
    student_ids = {sub.student_id for sub in subs}
    users = (
        db.query(User).filter(User.id.in_(student_ids)).all() if student_ids else []
    )
    names = {u.id: (u.full_name or u.email) for u in users}
    for sub in subs:
        sub.student_name = names.get(sub.student_id)
    return subs




@router.get("/submissions/{submission_id}", response_model=schemas.SubmissionWithGrade)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_submission(db, submission_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Submission not found")
    if current_user.role == "student":
        _check_submission_org(db, obj, current_user)
        if obj.student_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        return _student_submission_view(obj)

    # Преподаватель/админ — только владелец класса задания (SEC-2).
    require_submission_class_owner(db, obj, current_user)
    from models import User
    user = db.query(User).filter(User.id == obj.student_id).first()
    if user:
        obj.student_name = user.full_name or user.email
    return obj


@router.delete(
    "/submissions/{submission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    obj = crud.get_submission(db, submission_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Submission not found")

    if current_user.role == "student":
        _check_submission_org(db, obj, current_user)
        if obj.student_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        if obj.status == "graded":
            raise HTTPException(status_code=400, detail="Нельзя удалить уже проверенную сдачу")
    else:
        # Преподаватель удаляет сдачи только в своём классе (SEC-2).
        require_submission_class_owner(db, obj, current_user)

    from models import Grade
    db.query(Grade).filter(Grade.submission_id == submission_id).delete()
    # BE-10: убираем и файлы сдачи с диска до удаления записи (после delete
    # объект уже не отдаст file_urls). Очистка best-effort, ошибки не роняют запрос.
    from services.file_cleanup import delete_submission_files
    delete_submission_files(obj)
    db.delete(obj)
    db.commit()


@router.post(
    "/submissions/{submission_id}/grade",
    response_model=schemas.GradeResponse,
    status_code=status.HTTP_201_CREATED,
)
def save_grade(
    submission_id: int,
    body: schemas.GradeCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    sub = crud.get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    require_submission_class_owner(db, sub, current_user)  # SEC-2

    # BE-5: клампим балл в [0, max_score] — ИИ-путь это делал, ручной нет,
    # поэтому преподаватель мог выставить балл выше максимума и сломать проценты.
    assignment = crud.get_assignment(db, sub.assignment_id)
    max_score = assignment.max_score if assignment and assignment.max_score else 100
    clamped_score = max(0, min(body.score, max_score))

    crud.set_submission_status(db, submission_id, "graded")
    # graded_by выставляется сервером: ручную оценку нельзя выдать за ИИ-проверку
    grade = crud.create_or_update_grade(
        db=db,
        submission_id=submission_id,
        score=clamped_score,
        feedback=body.feedback,
        criteria_scores=body.criteria_scores,
        graded_by="teacher",
    )
    _notify_grade(sub, assignment, clamped_score, max_score)
    return grade


@router.get("/submissions/{submission_id}/grade", response_model=schemas.GradeResponse)
def get_grade(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    grade = crud.get_grade_by_submission(db, submission_id)
    if not grade:
        raise HTTPException(status_code=404, detail="Grade not found yet")

    sub = crud.get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if current_user.role == "student":
        _check_submission_org(db, sub, current_user)
        if sub.student_id != current_user.id:
            raise HTTPException(status_code=403, detail="Access denied")
        # Предложение ИИ (needs_review) — не финальная оценка, студент видит
        # её только после подтверждения учителем (graded_by становится "teacher").
        if grade.graded_by == "ai_suggested":
            raise HTTPException(status_code=404, detail="Grade not found yet")
    else:
        require_submission_class_owner(db, sub, current_user)  # SEC-2
    return grade


@router.patch("/submissions/{submission_id}/status")
def update_status(
    submission_id: int,
    new_status: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    allowed = {"submitted", "grading", "graded", "late", "needs_review"}
    if new_status not in allowed:
        raise HTTPException(status_code=422, detail=f"Status must be one of {allowed}")
    sub = crud.get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    require_submission_class_owner(db, sub, current_user)  # SEC-2
    # BE-10: нельзя откатить уже проверенную сдачу произвольным статусом —
    # оценка есть, а «grading/submitted» рассинхронил бы её с UI и переоценкой.
    if sub.status == "graded" and new_status != "graded":
        raise HTTPException(
            status_code=409,
            detail="Сдача уже проверена; удалите оценку, чтобы изменить статус.",
        )
    obj = crud.set_submission_status(db, submission_id, new_status)
    return {"id": obj.id, "status": obj.status}





@router.post(
    "/submissions/{submission_id}/ai-grade",
    response_model=schemas.AiGradeResult,
)
async def ai_grade_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    sub = crud.get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    require_submission_class_owner(db, sub, current_user)  # SEC-2

    _check_rate_limit(current_user.id)

    # BE-9: дневной бюджет токенов ИИ на организацию — отбиваем «дорогую»
    # лавину до обращения к модели.
    from services.ai_budget import can_spend
    if not can_spend(db, current_user.org_type):
        raise HTTPException(
            status_code=429,
            detail="Дневной лимит ИИ-проверок для организации исчерпан. Попробуйте завтра.",
        )

    assignment = crud.get_assignment(db, sub.assignment_id)
    criteria = _json.loads(assignment.criteria) if assignment.criteria else []

    if not criteria:
        raise HTTPException(
            status_code=400,
            detail="Нет критериев оценивания. Добавьте критерии в задание.",
        )

    crud.set_submission_status(db, submission_id, "grading")


    full_text = sub.text_content or ""

    all_urls: list = []
    if sub.file_urls:
        try:
            all_urls = _json.loads(sub.file_urls)
        except Exception:
            pass
    if not all_urls and sub.file_url:
        all_urls = [sub.file_url]

    # Фото рукописных работ идут отдельным vision-путём (grade_handwritten_
    # submission) с обязательной проверкой уверенности распознавания —
    # остальные файлы (docx/pdf/txt/...) читаются как раньше в full_text.
    image_urls = [u for u in all_urls if u.split("?")[0].rsplit(".", 1)[-1].lower() in _IMAGE_EXTS]
    doc_urls = [u for u in all_urls if u not in image_urls]

    from services.ai_grader import _fetch_file_text
    for i, url in enumerate(doc_urls):
        file_text = await _fetch_file_text(url)
        if file_text.strip():
            label = f"ФАЙЛ {i+1}" if len(doc_urls) > 1 else "СОДЕРЖИМОЕ ФАЙЛА"
            full_text = (
                (full_text + f"\n\n{label}:\n" + file_text).strip()
                if full_text
                else f"{label}:\n{file_text}"
            )

    if not full_text and not image_urls:
        full_text = f"[Студент сдал файл(ы), но прочитать не удалось: {', '.join(all_urls)}]"


    reference_urls: list = []

    if sub.variant_number:
        variant = crud_classes.get_variant_by_number(db, sub.assignment_id, sub.variant_number)
        if variant:
            try:
                import json as _json2
                var_urls = _json2.loads(variant.reference_solution_url) if variant.reference_solution_url.startswith('[') else None
                if isinstance(var_urls, list):
                    reference_urls.extend(var_urls)
                else:
                    reference_urls.append(variant.reference_solution_url)
            except Exception:
                reference_urls.append(variant.reference_solution_url)
        else:
            if assignment.reference_solution_url:
                try:
                    import json as _json2
                    urls = _json2.loads(assignment.reference_solution_url) if assignment.reference_solution_url.startswith('[') else None
                    if isinstance(urls, list):
                        reference_urls.extend(urls)
                    else:
                        reference_urls.append(assignment.reference_solution_url)
                except Exception:
                    reference_urls.append(assignment.reference_solution_url)
    else:
        if assignment.reference_solution_url:
            try:
                import json as _json2
                urls = _json2.loads(assignment.reference_solution_url) if assignment.reference_solution_url.startswith('[') else None
                if isinstance(urls, list):
                    reference_urls.extend(urls)
                else:
                    reference_urls.append(assignment.reference_solution_url)
            except Exception:
                reference_urls.append(assignment.reference_solution_url)


    # class_id лежит внутри JSON-поля body: сужаем выборку в SQL по подстроке,
    # точное совпадение перепроверяем в Python. Посты без class_id не берём.
    lecture_context = ""
    try:
        from models import Posts
        posts = (
            db.query(Posts)
            .filter(Posts.user_id != None)
            .filter(Posts.body.contains('"class_id"'))
            .all()
        )
        parts = []
        for p in posts:
            try:
                b = _json.loads(p.body)
                ptype = b.get("type", "")
                if ptype not in ("lecture", "material"):
                    continue
                class_id_in_body = b.get("class_id")
                if not class_id_in_body or int(class_id_in_body) != assignment.class_id:
                    continue
                content = (b.get("content") or b.get("description") or "")[:2000]
                block = f"### {p.title}\n{content}"
                parts.append(block)
            except Exception:
                continue
        lecture_context = "\n\n".join(parts[:5])
    except Exception:
        pass

    is_handwritten = bool(image_urls)
    try:
        if is_handwritten:
            reference_text = None
            if reference_urls:
                ref_parts = []
                for ref_url in reference_urls:
                    ref_content = await _fetch_file_text(ref_url)
                    if ref_content.strip() and not ref_content.startswith("["):
                        ref_parts.append(ref_content[:6000])
                if ref_parts:
                    reference_text = "\n\n---\n\n".join(ref_parts)

            result = await _ai_grade_handwritten(
                image_urls=image_urls,
                text=full_text,
                criteria=criteria,
                max_score=assignment.max_score,
                reference_text=reference_text,
                lecture_context=lecture_context if lecture_context else None,
            )
        else:
            result = await _ai_grade(
                text=full_text,
                file_url=None,
                criteria=criteria,
                max_score=assignment.max_score,
                reference_solution_url=None,
                reference_solution_urls=reference_urls if reference_urls else None,
                lecture_context=lecture_context if lecture_context else None,
            )
    except RuntimeError as e:
        crud.set_submission_status(db, submission_id, "submitted")
        raise HTTPException(status_code=502, detail=str(e))


    try:
        usage = result.pop("_usage", {})
        from models import AiUsageLog
        log = AiUsageLog(
            user_id=current_user.id,
            class_id=assignment.class_id,
            endpoint="ai-grade",
            org_type=current_user.org_type,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
        db.add(log)
        db.commit()
    except Exception:
        pass

    # Confidence теперь считает и текстовый путь (пустая/не по теме/нечитаемая
    # сдача), не только распознавание фото — гейт needs_review общий для всех
    # типов сдач.
    confidence = result.get("confidence")
    reasons = result.get("confidence_reasons")

    crud.set_submission_ai_confidence(db, submission_id, confidence, reasons)

    if confidence is None or confidence < AI_CONFIDENCE_THRESHOLD:
        # Уверенность ИИ в оценке недостаточна — оценку не показываем студенту,
        # сдача ждёт ручной проверки учителем (единый источник истины для
        # web/Flutter: оба читают статус/поля отсюда). Предложение ИИ сохраняем
        # как ai_suggested — учитель видит его в карточке ручной проверки и
        # может подтвердить как есть или изменить (POST .../grade перезаписывает
        # на graded_by="teacher").
        crud.set_submission_status(db, submission_id, "needs_review")
        grade = crud.create_or_update_grade(
            db=db,
            submission_id=submission_id,
            score=result["score"],
            feedback=result.get("feedback"),
            criteria_scores=result.get("criteria_scores"),
            graded_by="ai_suggested",
        )
        _notify_needs_review(sub, assignment)
        return schemas.AiGradeResult(
            status="needs_review",
            grade=grade,
            ai_confidence=confidence,
            ai_review_reasons=_json.dumps(reasons or [], ensure_ascii=False),
        )

    crud.set_submission_status(db, submission_id, "graded")
    grade = crud.create_or_update_grade(
        db=db,
        submission_id=submission_id,
        score=result["score"],
        feedback=result.get("feedback"),
        criteria_scores=result.get("criteria_scores"),
        graded_by="ai",
    )
    _notify_grade(sub, assignment, result["score"], assignment.max_score)
    return schemas.AiGradeResult(
        status="graded",
        grade=grade,
        ai_confidence=confidence,
        ai_review_reasons=_json.dumps(reasons, ensure_ascii=False) if reasons is not None else None,
    )