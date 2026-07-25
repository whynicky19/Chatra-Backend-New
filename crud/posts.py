from sqlalchemy.orm import Session
from models import Posts, User
from services.file_cleanup import delete_post_files, delete_replaced_post_cover

def create_new_post(db: Session, title: str, body: str, user_id: int) -> Posts:
    post = Posts(title=title, body=body, user_id=user_id)
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
