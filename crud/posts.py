import re

from sqlalchemy.orm import Session
from sqlalchemy import func

from models import Posts, User
from services.file_cleanup import delete_post_files, delete_replaced_post_cover

_LECTURE_TITLE_RE = re.compile(r"^\[LECTURE\]\[(\d+)\]")


def _next_lecture_position(db: Session, class_id: str, title: str) -> int:
    """Следующий порядковый номер лекции внутри класса — макс. существующий
    position среди постов с тем же префиксом заголовка + 1. Нумерация растёт
    монотонно даже если более ранние лекции были удалены, чтобы уже
    выданные пользователю номера ("2 лекция") не переехали на другой пост."""
    max_position = (
        db.query(func.max(Posts.position))
        .filter(Posts.title.like(f"[LECTURE][{class_id}]%"))
        .scalar()
    )
    return (max_position or 0) + 1


def create_new_post(db: Session, title: str, body: str, user_id: int) -> Posts:
    position = None
    m = _LECTURE_TITLE_RE.match(title or "")
    if m:
        position = _next_lecture_position(db, m.group(1), title)
    post = Posts(title=title, body=body, user_id=user_id, position=position)
    db.add(post)
    db.commit()
    db.refresh(post)
    return post

def get_post_by_id(db: Session, post_id: int):
    return db.query(Posts).filter(Posts.id == post_id).first()

def get_posts_for_user(db: Session, user_id: int):
    return db.query(Posts).filter(Posts.user_id == user_id).order_by(Posts.id.desc()).all()

def get_all_posts(db: Session, org_type: str = None, class_id: int = None,
                  limit: int = None, offset: int = 0):
    q = db.query(Posts).join(User, User.id == Posts.user_id)
    if org_type:
        q = q.filter(User.org_type == org_type)
    if class_id is not None:
        # Лекции класса маркируются префиксом заголовка — фильтруем на
        # сервере, чтобы клиент не скачивал все посты организации целиком.
        q = q.filter(Posts.title.like(f"[LECTURE][{class_id}]%"))
        # Лекции нужны в порядке "1, 2, 3..." (position), а не "новые сверху" —
        # иначе AI-репетитор класса не может связать "объясни 2 лекцию" с
        # правильным постом. Лекции без position (созданные до migrations/017)
        # уходят в конец, отсортированные по id как раньше.
        q = q.order_by(Posts.position.is_(None), Posts.position.asc(), Posts.id.asc())
    else:
        q = q.order_by(Posts.id.desc())
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()

def delete_post(db: Session, post_id: int) -> bool:
    post = get_post_by_id(db, post_id)
    if not post:
        return False
    # BE-10: обложка лекции (cover_image в body) — единственный файл поста.
    delete_post_files(post)
    db.delete(post)
    db.commit()
    return True

def update_post(db: Session, post_id: int, title: str, body: str) -> Posts:
    post = get_post_by_id(db, post_id)
    if not post:
        return None
    old_body = post.body
    post.title = title
    post.body = body
    db.commit()
    db.refresh(post)
    # BE-10: обложку заменили (или убрали) — старый файл больше не нужен.
    delete_replaced_post_cover(old_body, body)
    return post
