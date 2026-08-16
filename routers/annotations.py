"""Выделения (highlights) и заметки к материалам лекций.

Серверная истина, общая для приложения и сайта: выделение, сделанное на
телефоне, открывается на сайте и наоборот. Клиенты ничего не хранят локально
кроме кэша.

Приватность: выделения и заметки видит и правит ТОЛЬКО их автор — даже
преподаватель класса не имеет к ним доступа (это личные пометки студента).
Проверка доступа к классу нужна лишь при создании, чтобы нельзя было завести
пометку в чужой лекции.
"""
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from crud import posts as crud_posts
from db import get_db
from deps import get_current_user
from models import Annotation, Posts
from permissions import require_class_access
from utils.time import utcnow

router = APIRouter(prefix="/annotations", tags=["Annotations"])

COLORS = {"yellow", "green", "blue", "red"}

# Границы полей. Выделение — это фрагмент для чтения и вопроса к ИИ, а не
# способ залить в базу мегабайт текста одним запросом.
MAX_TEXT = 4000
MAX_CONTEXT = 200
MAX_COMMENT = 2000
MAX_LIST_LIMIT = 500

SelectedText = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
ContextText = Annotated[str, Field(max_length=MAX_CONTEXT)]
Comment = Annotated[str, Field(max_length=MAX_COMMENT)]


class AnnotationCreate(BaseModel):
    lecture_id: int
    class_id: int
    # -1 — текст самой лекции (вложения нет), иначе индекс файла в лекции.
    file_index: int = -1
    # Страница PDF (1..N); 0 — документ без страниц.
    page: int = Field(default=0, ge=0)
    selected_text: SelectedText
    # Якорь по тексту вокруг фрагмента — по нему выделение находится заново,
    # если смещения не сошлись (см. models.Annotation).
    prefix: ContextText = ""
    suffix: ContextText = ""
    start_offset: int = Field(default=0, ge=0)
    end_offset: int = Field(default=0, ge=0)
    color: str = "yellow"
    comment: Optional[Comment] = None


class AnnotationUpdate(BaseModel):
    color: Optional[str] = None
    comment: Optional[Comment] = None


class AnnotationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    lecture_id: int
    class_id: int
    file_index: int
    page: int
    selected_text: str
    prefix: str
    suffix: str
    start_offset: int
    end_offset: int
    color: str
    comment: Optional[str]
    created_at: datetime
    updated_at: datetime
    # Название лекции — чтобы список «Мои выделения» не делал запрос за каждой
    # лекцией отдельно (на сайте и в приложении он показывает лекцию строкой).
    lecture_title: Optional[str] = None


def _check_color(color: str) -> str:
    if color not in COLORS:
        raise HTTPException(
            status_code=422,
            detail=f"color must be one of: {', '.join(sorted(COLORS))}",
        )
    return color


def _lecture_class_id(post: Posts) -> Optional[int]:
    """Класс лекции берётся из заголовка ('[LECTURE][{class_id}] ...') — другой
    привязки поста к классу в схеме нет (см. crud/posts.py)."""
    m = crud_posts._LECTURE_TITLE_RE.match(post.title or "")
    return int(m.group(1)) if m else None


def _lecture_title(post: Optional[Posts]) -> Optional[str]:
    if post is None:
        return None
    return crud_posts._LECTURE_TITLE_STRIP_RE.sub("", post.title or "").strip() or None


def _serialize(row: Annotation, title: Optional[str]) -> AnnotationResponse:
    return AnnotationResponse(**{
        **{c.name: getattr(row, c.name) for c in Annotation.__table__.columns},
        "lecture_title": title,
    })


def _own_annotation(db: Session, annotation_id: int, user_id: int) -> Annotation:
    row = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    # 404, а не 403, на чужую запись: существование чужих пометок — не то, что
    # стоит подтверждать в ответе.
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Annotation not found")
    return row


@router.get("", response_model=List[AnnotationResponse])
def list_annotations(
    lecture_id: Optional[int] = Query(default=None),
    class_id: Optional[int] = Query(default=None),
    # Инкрементальная синхронизация: отдать только изменённое после метки.
    updated_after: Optional[datetime] = Query(default=None),
    limit: int = Query(default=MAX_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Свои выделения: всей лекции, всего класса или вообще все («Мои выделения»)."""
    q = db.query(Annotation).filter(Annotation.user_id == current_user.id)
    if lecture_id is not None:
        q = q.filter(Annotation.lecture_id == lecture_id)
    if class_id is not None:
        q = q.filter(Annotation.class_id == class_id)
    if updated_after is not None:
        q = q.filter(Annotation.updated_at > updated_after)
    rows = (
        q.order_by(Annotation.lecture_id, Annotation.file_index,
                   Annotation.page, Annotation.start_offset, Annotation.id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    titles = _titles_for(db, {r.lecture_id for r in rows})
    return [_serialize(r, titles.get(r.lecture_id)) for r in rows]


def _titles_for(db: Session, lecture_ids: set) -> dict:
    if not lecture_ids:
        return {}
    rows = db.query(Posts.id, Posts.title).filter(Posts.id.in_(lecture_ids)).all()
    return {
        pid: crud_posts._LECTURE_TITLE_STRIP_RE.sub("", title or "").strip() or None
        for pid, title in rows
    }


@router.post("", response_model=AnnotationResponse, status_code=201)
def create_annotation(
    body: AnnotationCreate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_color(body.color)
    if body.end_offset < body.start_offset:
        raise HTTPException(status_code=422, detail="end_offset must be >= start_offset")

    post = crud_posts.get_post_by_id(db, body.lecture_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Lecture not found")

    # Класс берём из самой лекции, а не из тела запроса: иначе клиент мог бы
    # приписать пометку к классу, к которому лекция не относится, и утащить её
    # контекст в ИИ-чат чужого предмета.
    lecture_class_id = _lecture_class_id(post)
    if lecture_class_id is None:
        raise HTTPException(status_code=422, detail="Post is not a lecture")
    require_class_access(db, lecture_class_id, current_user)

    row = Annotation(
        user_id=current_user.id,
        lecture_id=post.id,
        class_id=lecture_class_id,
        file_index=body.file_index,
        page=body.page,
        selected_text=body.selected_text,
        prefix=body.prefix,
        suffix=body.suffix,
        start_offset=body.start_offset,
        end_offset=body.end_offset,
        color=body.color,
        comment=body.comment,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize(row, _lecture_title(post))


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
def update_annotation(
    annotation_id: int,
    body: AnnotationUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Меняются только цвет и заметка: сам фрагмент и его позиция — свойства
    сделанного выделения, для другого места создаётся новое."""
    row = _own_annotation(db, annotation_id, current_user.id)
    if body.color is not None:
        row.color = _check_color(body.color)
    if body.comment is not None:
        row.comment = body.comment or None
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return _serialize(row, _lecture_title(crud_posts.get_post_by_id(db, row.lecture_id)))


@router.delete("/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _own_annotation(db, annotation_id, current_user.id)
    db.delete(row)
    db.commit()
