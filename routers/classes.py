from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas
from crud import classes as crud
from models import Class, class_members
from db import get_db
from deps import get_current_user, get_current_teacher
from services.image_storage import convert_cover_if_data_uri
from services.rate_limit import RateLimiter
from sqlalchemy import func

router = APIRouter(prefix="/classes", tags=["Classes"])


_join_limiter = RateLimiter(max_calls=10, window_seconds=60)


def _check_join_rate_limit(key):
    _join_limiter.check(key, detail="too_many_attempts")


def _can_view_invite_code(obj: Class, current_user) -> bool:
    return current_user.role == "admin" or obj.created_by == current_user.id


def _to_class_response(obj: Class, current_user, member_count: Optional[int] = None) -> schemas.ClassResponse:
    resp = schemas.ClassResponse.model_validate(obj)
    if member_count is not None:
        resp.member_count = member_count
    if not _can_view_invite_code(obj, current_user):
        resp.invite_code = None
    return resp


def _member_counts(db: Session, class_ids: List[int]) -> dict:
    """Количество участников по классам одним агрегатным запросом — вместо
    len(c.members), который лениво тащил полные строки всех участников
    каждого класса (N+1)."""
    if not class_ids:
        return {}
    rows = (
        db.query(class_members.c.class_id, func.count(class_members.c.user_id))
        .filter(class_members.c.class_id.in_(class_ids))
        .group_by(class_members.c.class_id)
        .all()
    )
    return dict(rows)




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
    if current_user.role == "student":
        classes = db.query(crud.Class).filter(
            crud.Class.members.any(id=current_user.id),
            crud.Class.org_type == current_user.org_type,
        ).order_by(crud.Class.created_at.desc()).all()
    else:
        teacher_id = current_user.id if my_only else None
        classes = crud.get_all_classes(db, teacher_id=teacher_id, org_type=current_user.org_type)
    counts = _member_counts(db, [c.id for c in classes])
    result = [_to_class_response(c, current_user, member_count=counts.get(c.id, 0)) for c in classes]
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
    return _to_class_response(obj, current_user, member_count=len(obj.members))


@router.get("/{class_id}", response_model=schemas.ClassResponse)
def get_class(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    return _to_class_response(obj, current_user, member_count=len(obj.members))


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
    return _to_class_response(obj, current_user, member_count=len(obj.members))


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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    obj = crud.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Класс не найден")
    return crud.get_members(db, class_id)


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

    ok = crud.add_member(db, obj.id, current_user.id)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось вступить")
    return _to_class_response(obj, current_user, member_count=len(obj.members))


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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):

    obj = crud.get_class(db, class_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Класс не найден")
    rows = crud.get_student_rating(db, class_id=class_id, org_type=current_user.org_type)
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