from typing import List, Optional

from fastapi import APIRouter, status, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import schemas
from db import get_db
from crud import posts as crud_posts
from crud import classes as crud_classes
from deps import get_current_user
from models import Posts, User, post_enrollments
from permissions import require_class_access, student_class_ids

router = APIRouter(prefix="/posts", tags=["posts"])


def _get_post_or_404(db: Session, post_id: int) -> Posts:
    post = crud_posts.get_post_by_id(db=db, post_id=post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _check_post_org(db: Session, post: Posts, current_user) -> None:

    creator = db.query(User).filter(User.id == post.user_id).first()
    if not creator or creator.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Post not found")


def _convert_body_cover(body: str) -> str:
    """Легаси-посты «типа класс» несут base64-обложку прямо в JSON-теле
    (100-150 КБ на пост), и она уезжает в каждый ответ /posts/. Здесь
    data-URI один раз сохраняется файлом в uploads/, а в теле остаётся URL."""
    if not body or '"cover_image":"data:' not in body.replace(" ", ""):
        return body
    try:
        import json
        from services.image_storage import convert_cover_if_data_uri
        parsed = json.loads(body)
        if isinstance(parsed, dict) and isinstance(parsed.get("cover_image"), str):
            parsed["cover_image"] = convert_cover_if_data_uri(parsed["cover_image"])
            return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        pass
    return body


@router.post("/create", response_model=schemas.PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    post: schemas.PostCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return crud_posts.create_new_post(db=db, title=post.title, body=_convert_body_cover(post.body), user_id=current_user.id)


@router.get("/", response_model=List[schemas.PostResponse])
def get_posts_for_user(
    class_id: Optional[int] = None,
    limit: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # SEC: раньше тут не было никакой проверки членства — любой
    # авторизованный пользователь организации мог запросить лекции ЛЮБОГО
    # класса (по известному/подобранному class_id) или вообще все посты
    # организации без class_id. Модель прав та же, что у /assignments/ и
    # /classes/: teacher/admin видят всё в организации, студент — только
    # свои классы.
    allowed_class_ids = None
    if class_id is not None:
        cls = crud_classes.get_class(db, class_id)
        if cls and cls.org_type != current_user.org_type:
            raise HTTPException(status_code=404, detail="Class not found")
        require_class_access(db, class_id, current_user)
    elif current_user.role == "student":
        allowed_class_ids = student_class_ids(db, current_user.id, current_user.org_type)
    return crud_posts.get_all_posts(
        db=db,
        org_type=current_user.org_type,
        class_id=class_id,
        limit=limit,
        offset=offset,
        allowed_class_ids=allowed_class_ids,
    )


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    _check_post_org(db, post, current_user)
    if post.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to delete this post")
    crud_posts.delete_post(db=db, post_id=post_id)


@router.post("/{post_id}/join", status_code=200)
def join_post_class(
    post_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    _check_post_org(db, post, current_user)
    exists = db.execute(
        post_enrollments.select().where(
            post_enrollments.c.post_id == post_id,
            post_enrollments.c.user_id == current_user.id,
        )
    ).first()
    if not exists:
        db.execute(post_enrollments.insert().values(post_id=post_id, user_id=current_user.id))
        db.commit()
    return {"ok": True}


@router.delete("/{post_id}/leave", status_code=200)
def leave_post_class(
    post_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)
    _check_post_org(db, post, current_user)
    db.execute(
        post_enrollments.delete().where(
            post_enrollments.c.post_id == post_id,
            post_enrollments.c.user_id == current_user.id,
        )
    )
    db.commit()
    return {"ok": True}


@router.put("/{post_id}", response_model=schemas.PostResponse)
def update_post(
    post_id: int,
    post: schemas.PostCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    existing = _get_post_or_404(db, post_id)
    _check_post_org(db, existing, current_user)
    if existing.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    return crud_posts.update_post(db=db, post_id=post_id, title=post.title, body=_convert_body_cover(post.body))
