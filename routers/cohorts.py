"""Потоки и переход учебного года. Всё здесь — только для
преподавателя-владельца класса (или админа): обычные ученики не видят
чужие потоки и архивы, где не состояли."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import schemas
from crud import cohorts as crud_cohorts
from db import get_db
from deps import get_current_teacher
from models import Assignment, Class, Cohort, Deadline, Submission, cohort_students

router = APIRouter(tags=["Cohorts"])


def _get_owned_class(db: Session, class_id: int, current_user) -> Class:
    obj = db.query(Class).filter(Class.id == class_id).first()
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Предмет не найден")
    if current_user.role != "admin" and obj.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Только владелец предмета")
    return obj


def _get_owned_cohort(db: Session, cohort_id: int, current_user) -> Cohort:
    cohort = crud_cohorts.get_cohort(db, cohort_id)
    if not cohort:
        raise HTTPException(status_code=404, detail="Поток не найден")
    _get_owned_class(db, cohort.class_id, current_user)
    return cohort




@router.get("/rollover/preview", response_model=List[schemas.RolloverPreviewItem])
def rollover_preview(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Экран подтверждения перехода: классы владельца (или, для админа, все
    классы его организации) с rotation_mode='yearly' и активным потоком."""
    filters = [
        Class.rotation_mode == "yearly",
        Class.org_type == current_user.org_type,
    ]
    if current_user.role != "admin":
        filters.append(Class.created_by == current_user.id)
    classes = (
        db.query(Class)
        .filter(*filters)
        .order_by(Class.created_at.desc())
        .all()
    )
    items = []
    for cls in classes:
        cohort = crud_cohorts.get_active_cohort(db, cls.id)
        if cohort is None:
            continue
        student_count = db.query(func.count(cohort_students.c.student_id)).filter(
            cohort_students.c.cohort_id == cohort.id
        ).scalar()
        assignment_count = db.query(func.count(Assignment.id)).filter(
            Assignment.class_id == cls.id
        ).scalar()
        items.append(schemas.RolloverPreviewItem(
            class_id=cls.id,
            class_name=cls.name,
            cohort_id=cohort.id,
            academic_year=cohort.academic_year,
            student_count=student_count or 0,
            assignment_count=assignment_count or 0,
        ))
    return items


@router.post("/rollover", response_model=List[schemas.RolloverResultItem])
def rollover(
    body: schemas.RolloverRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Переход выбранных классов на новый учебный год. Транзакция на каждый
    класс: ошибка одного не откатывает остальные, повторный вызов для уже
    перенесённого класса возвращает already_rolled и ничего не ломает."""
    results = []
    for class_id in body.class_ids:
        cls = _get_owned_class(db, class_id, current_user)
        try:
            result = crud_cohorts.rollover_class(
                db, cls, body.new_academic_year, body.new_start_date
            )
            db.commit()
        except IntegrityError:
            # Параллельный rollover того же класса упёрся в partial unique
            # index «один активный поток» — данные целы.
            db.rollback()
            result = {"class_id": class_id, "status": "conflict"}
        results.append(schemas.RolloverResultItem(**result))
    return results




@router.get("/classes/{class_id}/cohorts", response_model=List[schemas.CohortResponse])
def list_class_cohorts(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Все потоки класса (просмотр архивов) — только владелец."""
    _get_owned_class(db, class_id, current_user)
    cohorts = crud_cohorts.get_cohorts_for_class(db, class_id)
    counts = dict(
        db.query(cohort_students.c.cohort_id, func.count(cohort_students.c.student_id))
        .filter(cohort_students.c.cohort_id.in_([c.id for c in cohorts] or [0]))
        .group_by(cohort_students.c.cohort_id)
        .all()
    )
    result = []
    for c in cohorts:
        resp = schemas.CohortResponse.model_validate(c)
        resp.student_count = counts.get(c.id, 0)
        result.append(resp)
    return result


@router.patch("/classes/{class_id}/rotation-mode", response_model=schemas.ClassResponse)
def set_rotation_mode(
    class_id: int,
    body: schemas.RotationModeUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = _get_owned_class(db, class_id, current_user)
    obj.rotation_mode = body.rotation_mode
    if body.rotation_mode == "yearly" and crud_cohorts.get_active_cohort(db, class_id) is None:
        crud_cohorts.create_initial_cohort(db, class_id)
    db.commit()
    db.refresh(obj)
    return schemas.ClassResponse.model_validate(obj)




def _deadline_response(d: Deadline, title: Optional[str], was_shifted: bool = False) -> schemas.DeadlineResponse:
    resp = schemas.DeadlineResponse.model_validate(d)
    resp.assignment_title = title
    resp.was_shifted = was_shifted
    # Для no_deadline=True в БД лежит placeholder 2099-12-31 (из-за NOT NULL)
    # — маскируем обратно в None, чтобы клиент не рендерил фантомную дату.
    if d.no_deadline:
        resp.due_date = None
    return resp


def _was_shifted(db: Session, d: Deadline) -> bool:
    """Дедлайн был автоматически сдвинут (rollover), если в архивном потоке
    того же класса уже была запись Deadline для этого задания — её скопировали
    в новый поток со сдвигом. Используется для бейджа 'проверьте дату' в UI."""
    class_id = db.query(Cohort.class_id).filter(Cohort.id == d.cohort_id).scalar()
    if class_id is None:
        return False
    return (
        db.query(Deadline.id)
        .join(Cohort, Cohort.id == Deadline.cohort_id)
        .filter(
            Cohort.class_id == class_id,
            Cohort.status == "archived",
            Deadline.assignment_id == d.assignment_id,
        )
        .first()
        is not None
    )


@router.get("/cohorts/{cohort_id}/deadlines", response_model=List[schemas.DeadlineResponse])
def list_cohort_deadlines(
    cohort_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Дедлайны потока для экрана проверки после rollover — только владелец."""
    _get_owned_cohort(db, cohort_id, current_user)
    rows = (
        db.query(Deadline, Assignment.title)
        .join(Assignment, Assignment.id == Deadline.assignment_id)
        .filter(Deadline.cohort_id == cohort_id)
        .order_by(Deadline.due_date)
        .all()
    )
    # was_shifted считаем одним запросом: ID архивных дедлайнов того же класса.
    # Для каждой строки проверяем, есть ли её assignment_id в этом наборе.
    klass_id = db.query(Cohort.class_id).filter(Cohort.id == cohort_id).scalar()
    archived_aids: set = set()
    if klass_id is not None:
        archived_aids = {
            aid for (aid,) in db.query(Deadline.assignment_id)
            .join(Cohort, Cohort.id == Deadline.cohort_id)
            .filter(Cohort.class_id == klass_id, Cohort.status == "archived")
            .all()
        }
    return [
        _deadline_response(d, title, was_shifted=(d.assignment_id in archived_aids))
        for d, title in rows
    ]


@router.patch("/deadlines/{deadline_id}", response_model=schemas.DeadlineResponse)
def update_deadline(
    deadline_id: int,
    body: schemas.DeadlineUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Правка/публикация одного дедлайна потока — только владелец."""
    d = db.query(Deadline).filter(Deadline.id == deadline_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Дедлайн не найден")
    _get_owned_cohort(db, d.cohort_id, current_user)
    # BE-cohort-unpublish: снять публикацию нельзя, если по дедлайну уже есть
    # сдачи — иначе задание исчезнет из выдачи студента (is_hidden_for_user),
    # но сдачи останутся в БД без автоматической проверки (deadline_checker
    # фильтрует is_published==True). Возвращаем 409 — учитель должен либо
    # удалить сдачи, либо оставить как есть.
    if body.is_published is False and d.is_published:
        has_submissions = (
            db.query(Submission.id)
            .filter(Submission.deadline_id == d.id)
            .first()
            is not None
        )
        if has_submissions:
            raise HTTPException(
                status_code=409,
                detail="Нельзя снять публикацию: по этому дедлайну уже есть сдачи",
            )
    if body.due_date is not None:
        d.due_date = body.due_date
    if body.is_published is not None:
        d.is_published = body.is_published
    if body.no_deadline is not None:
        d.no_deadline = body.no_deadline
    db.commit()
    db.refresh(d)
    title = db.query(Assignment.title).filter(Assignment.id == d.assignment_id).scalar()
    return _deadline_response(d, title, was_shifted=_was_shifted(db, d))


@router.patch("/cohorts/{cohort_id}/deadlines/publish-all")
def publish_all_deadlines(
    cohort_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    """Массовая публикация всех черновиков дедлайнов потока — только владелец."""
    _get_owned_cohort(db, cohort_id, current_user)
    updated = (
        db.query(Deadline)
        .filter(Deadline.cohort_id == cohort_id, Deadline.is_published == False)
        .update({Deadline.is_published: True}, synchronize_session=False)
    )
    db.commit()
    return {"published": updated}
