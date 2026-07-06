from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas
from crud import classes as crud
from crud import cohorts as crud_cohorts
from models import Class, Cohort, class_members, cohort_students
from db import get_db
from deps import get_current_user, get_current_teacher
from services.image_storage import convert_cover_if_data_uri
from services.rate_limit import RateLimiter
from sqlalchemy import func, case

router = APIRouter(prefix="/classes", tags=["Classes"])


_join_limiter = RateLimiter(max_calls=10, window_seconds=60)


def _check_join_rate_limit(key):
    _join_limiter.check(key, detail="too_many_attempts")


def _can_view_invite_code(obj: Class, current_user) -> bool:
    return current_user.role == "admin" or obj.created_by == current_user.id


def _to_class_response(obj: Class, current_user, member_count: Optional[int] = None,
                       is_archived: bool = False) -> schemas.ClassResponse:
    resp = schemas.ClassResponse.model_validate(obj)
    if member_count is not None:
        resp.member_count = member_count
    resp.is_archived_for_user = is_archived
    if not _can_view_invite_code(obj, current_user):
        resp.invite_code = None
    return resp


def _member_counts(db: Session, class_ids: List[int]) -> dict:
    """Количество участников по классам одним агрегатным запросом — вместо
    len(c.members), который лениво тащил полные строки всех участников
    каждого класса (N+1). Считаем по АКТИВНОМУ потоку; классы без активного
    потока — по легаси class_members."""
    if not class_ids:
        return {}
    rows = (
        db.query(Cohort.class_id, func.count(cohort_students.c.student_id))
        .outerjoin(cohort_students, cohort_students.c.cohort_id == Cohort.id)
        .filter(Cohort.class_id.in_(class_ids), Cohort.status == "active")
        .group_by(Cohort.class_id)
        .all()
    )
    counts = dict(rows)
    missing = [cid for cid in class_ids if cid not in counts]
    if missing:
        rows = (
            db.query(class_members.c.class_id, func.count(class_members.c.user_id))
            .filter(class_members.c.class_id.in_(missing))
            .group_by(class_members.c.class_id)
            .all()
        )
        counts.update(dict(rows))
    return counts


def _require_cohort_of_class(db: Session, obj: Class, cohort_id: int, current_user) -> Cohort:
    """Просмотр произвольного (в т.ч. архивного) потока — только владелец
    класса или админ; обычные ученики чужие потоки и архивы не видят."""
    if not (current_user.role == "admin" or obj.created_by == current_user.id):
        raise HTTPException(status_code=403, detail="Только владелец класса может просматривать потоки")
    cohort = crud_cohorts.get_cohort(db, cohort_id)
    if not cohort or cohort.class_id != obj.id:
        raise HTTPException(status_code=404, detail="Поток не найден")
    return cohort




@router.get("/all", response_model=List[schemas.ClassResponse])
def list_all_classes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    classes = crud.get_all_classes(db, org_type=current_user.org_type)
    counts = _member_counts(db, [c.id for c in classes])
    result = []
    for c in classes:
        resp = schemas.ClassResponse.model_validate(c)
        resp.member_count = counts.get(c.id, 0)
        resp.invite_code = None
        result.append(resp)
    return result


@router.get("/", response_model=List[schemas.ClassResponse])
def list_classes(
    my_only: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    archived_map: dict = {}
    if current_user.role == "student":
        # Классы, где ученик состоит хоть в каком-то потоке. Активность
        # хотя бы одного его потока определяет is_archived_for_user.
        rows = (
            db.query(
                crud.Class,
                func.max(case((Cohort.status == "active", 1), else_=0)).label("has_active"),
            )
            .join(Cohort, Cohort.class_id == crud.Class.id)
            .join(cohort_students, cohort_students.c.cohort_id == Cohort.id)
            .filter(
                cohort_students.c.student_id == current_user.id,
                crud.Class.org_type == current_user.org_type,
            )
            .group_by(crud.Class.id)
            .all()
        )
        classes = [c for c, _ in rows]
        archived_map = {c.id: not bool(has_active) for c, has_active in rows}
        # Легаси: членство только в class_members (классы без потоков).
        legacy = db.query(crud.Class).filter(
            crud.Class.members.any(id=current_user.id),
            crud.Class.org_type == current_user.org_type,
        ).all()
        for c in legacy:
            if c.id not in archived_map:
                classes.append(c)
                archived_map[c.id] = False
        classes.sort(key=lambda c: c.created_at, reverse=True)
    else:
        teacher_id = current_user.id if my_only else None
        classes = crud.get_all_classes(db, teacher_id=teacher_id, org_type=current_user.org_type)
    counts = _member_counts(db, [c.id for c in classes])
    result = [
        _to_class_response(c, current_user, member_count=counts.get(c.id, 0),
                           is_archived=archived_map.get(c.id, False))
        for c in classes
    ]
    return result


@router.post("/", response_model=schemas.ClassResponse, status_code=status.HTTP_201_CREATED)
def create_class(
    body: schemas.ClassCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.create_class(db, name=body.name, description=body.description,
                            created_by=current_user.id,
                            org_type=current_user.org_type,
                            cover_image=convert_cover_if_data_uri(body.cover_image),
                            teacher=body.teacher,
                            period=body.period)
    return _to_class_response(obj, current_user, member_count=0)


@router.get("/lookup-by-code", response_model=schemas.ClassResponse)
def lookup_class_by_code(
    code: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Read-only check so the client can show 'class found / not found' while
    the user is still typing a code, without joining and without exposing the
    invite_code itself (masked by _to_class_response, same as everywhere else).
    Rate-limited like the join endpoints since it's still a code-guessing
    oracle (existence only, no invite_code leak)."""
    _check_join_rate_limit(current_user.id)

    obj = crud.get_class_by_invite_code(db, code.strip().upper())
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="class_not_found")
    counts = _member_counts(db, [obj.id])
    return _to_class_response(obj, current_user, member_count=counts.get(obj.id, 0))


@router.get("/{class_id}", response_model=schemas.ClassResponse)
def get_class(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    is_archived = (
        crud_cohorts.is_archived_for_user(db, class_id, current_user.id)
        if current_user.role == "student"
        else False
    )
    counts = _member_counts(db, [obj.id])
    return _to_class_response(obj, current_user, member_count=counts.get(obj.id, 0),
                              is_archived=is_archived)


@router.put("/{class_id}", response_model=schemas.ClassResponse)
def update_class(
    class_id: int,
    body: schemas.ClassUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    data = body.model_dump(exclude_none=True)
    if "cover_image" in data:
        data["cover_image"] = convert_cover_if_data_uri(data["cover_image"])
    obj = crud.update_class(db, class_id, data)
    counts = _member_counts(db, [obj.id])
    return _to_class_response(obj, current_user, member_count=counts.get(obj.id, 0))


@router.delete("/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    crud.delete_class(db, class_id)




@router.get("/{class_id}/members", response_model=List[schemas.UserResponse])
def get_members(
    class_id: int,
    cohort_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Ученики активного потока; cohort_id — просмотр прошлых лет
    (только владелец класса или админ)."""
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    if cohort_id is not None:
        _require_cohort_of_class(db, obj, cohort_id, current_user)
    return crud.get_members(db, class_id, cohort_id=cohort_id)


def _require_active_cohort_for_join(db: Session, class_id: int) -> None:
    """Вступление возможно только в активный поток. Классы совсем без потоков
    (база без бэкфилла) работают по-старому через class_members."""
    if crud_cohorts.get_active_cohort(db, class_id) is None and crud_cohorts.get_cohorts_for_class(db, class_id):
        raise HTTPException(status_code=409, detail="no_active_cohort")


@router.post("/{class_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(
    class_id: int,
    body: schemas.ClassMemberAdd,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    _require_active_cohort_for_join(db, class_id)
    ok = crud.add_member(db, class_id, body.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"message": "Участник добавлен"}


@router.post("/{class_id}/join", status_code=status.HTTP_200_OK)
def join_class(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Класс не найден")

    if obj.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нельзя вступить в класс другой организации")

    _require_active_cohort_for_join(db, class_id)
    ok = crud.add_member(db, class_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось вступить")
    return {"message": "Вы вступили в класс"}


@router.post("/join-by-code", response_model=schemas.ClassResponse, status_code=status.HTTP_200_OK)
def join_class_by_code(
    body: schemas.ClassJoinByCode,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _check_join_rate_limit(current_user.id)

    code = body.code.strip().upper()
    obj = crud.get_class_by_invite_code(db, code)
    # Cross-org codes are treated as not found so a guessed/leaked code from
    # another organization can't be used to probe for its existence.
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="class_not_found")

    _require_active_cohort_for_join(db, obj.id)
    ok = crud.add_member(db, obj.id, current_user.id)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось вступить")
    counts = _member_counts(db, [obj.id])
    return _to_class_response(obj, current_user, member_count=counts.get(obj.id, 0))


@router.post("/{class_id}/regenerate-code", response_model=schemas.InviteCodeResponse)
def regenerate_code(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    if not _can_view_invite_code(obj, current_user):
        raise HTTPException(status_code=403, detail="Только владелец класса или администратор может обновить код приглашения")

    obj = crud.regenerate_invite_code(db, class_id)
    return schemas.InviteCodeResponse(invite_code=obj.invite_code)


@router.delete("/{class_id}/leave", status_code=status.HTTP_200_OK)
def leave_class(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    crud.remove_member(db, class_id, current_user.id)
    return {"message": "Вы покинули класс"}


@router.delete("/{class_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    class_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    crud.remove_member(db, class_id, user_id)




@router.get("/{class_id}/rating", response_model=schemas.StudentRatingResponse)
def class_rating(
    class_id: int,
    cohort_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Рейтинг по активному потоку; cohort_id — прошлые годы
    (только владелец класса или админ)."""
    obj = crud.get_class(db, class_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Класс не найден")
    if cohort_id is not None:
        _require_cohort_of_class(db, obj, cohort_id, current_user)
    rows = crud.get_student_rating(db, class_id=class_id, org_type=current_user.org_type,
                                   cohort_id=cohort_id)
    return schemas.StudentRatingResponse(
        class_id=class_id,
        ratings=[schemas.StudentRatingEntry(**r) for r in rows],
    )



rating_router = APIRouter(tags=["Rating"])


@rating_router.get("/rating", response_model=schemas.StudentRatingResponse)
def global_rating(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rows = crud.get_student_rating(db, class_id=None, org_type=current_user.org_type)
    return schemas.StudentRatingResponse(
        class_id=None,
        ratings=[schemas.StudentRatingEntry(**r) for r in rows],
    )