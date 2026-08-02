import json
import re

from sqlalchemy.orm import Session
from sqlalchemy import func

from models import Posts, User
from services.file_cleanup import delete_post_files, delete_replaced_post_cover

_LECTURE_TITLE_RE = re.compile(r"^\[LECTURE\]\[(\d+)\]")
_LECTURE_TITLE_STRIP_RE = re.compile(r"^\[LECTURE\]\[\d+\]\s*")


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
    if m:
        # Инжест в RAG (services/rag_ingest.py) — фоново, не блокирует ответ
        # на создание поста. Не-лекционные посты (m is None) не индексируем.
        from services.rag_ingest import ingest_lecture_bg
        ingest_lecture_bg(post.id)
    return post

def get_post_by_id(db: Session, post_id: int):
    return db.query(Posts).filter(Posts.id == post_id).first()

def get_posts_for_user(db: Session, user_id: int):
    return db.query(Posts).filter(Posts.user_id == user_id).order_by(Posts.id.desc()).all()

def get_all_posts(db: Session, org_type: str = None, class_id: int = None,
                  limit: int = None, offset: int = 0,
                  exclude_author_ids=None):
    q = db.query(Posts).join(User, User.id == Posts.user_id)
    if org_type:
        q = q.filter(User.org_type == org_type)
    # Модерация UGC: контент из блок-листа не отдаём вообще. Клиентского
    # скрытия недостаточно — локальный список теряется при переустановке.
    if exclude_author_ids:
        q = q.filter(~Posts.user_id.in_(list(exclude_author_ids)))
    if class_id is not None:
        # Лекции класса маркируются префиксом заголовка — фильтруем на
        # сервере, чтобы клиент не скачивал все посты организации целиком.
        q = q.filter(Posts.title.like(f"[LECTURE][{class_id}]%"))
        # Лекции нужны в порядке "1, 2, 3..." (position), а не "новые сверху" —
        # иначе AI-репетитор класса не может связать "объясни 2 лекцию" с
        # правильным постом. Лекции без position (созданные до migrations/017)
        # хронологически ВСЕГДА раньше пронумерованных — NULLS FIRST, а не
        # LAST: раньше здесь стоял `position.is_(None), position.asc()`,
        # который клал безномерные лекции в КОНЕЦ, а не в начало. Для класса,
        # где такие лекции соседствуют с новыми (уже пронумерованными),
        # новая лекция получала position=1 и заскакивала перед старыми —
        # порядок вида "2, 1, 3" вместо "1, 2, 3" (после разворота на клиенте
        # для "новые сверху" это ломало и его: "3, 1, 2" вместо "3, 2, 1").
        q = q.order_by(Posts.position.asc().nulls_first(), Posts.id.asc())
    else:
        q = q.order_by(Posts.id.desc())
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return q.all()

def get_lecture_context(db: Session, class_id: int, limit: int = 5, max_chars: int = 2000) -> str:
    """Текст последних лекций класса для промпта ИИ (grading/тьютор).

    Использует ту же маркировку лекций (заголовок '[LECTURE][{class_id}]...'),
    что create_new_post/get_all_posts. Раньше вызывающий код (ai-grade в
    routers/assignments.py) искал в body несуществующие поля
    {"type": "lecture", "class_id": ...} — их не пишет ни один клиент (лекции
    хранят class_id только в префиксе заголовка, а body — это {"content"|
    "description", "files"}), поэтому lecture_context для проверки ИИ всегда
    получался пустым. Ошибки БД/парсинга не должны ломать проверку — просто
    возвращаем то, что удалось собрать."""
    try:
        posts = get_all_posts(db, class_id=class_id, limit=limit)
    except Exception:
        return ""
    parts = []
    for p in posts:
        try:
            body = json.loads(p.body)
            content = (body.get("content") or body.get("description") or "").strip()
        except Exception:
            content = (p.body or "").strip()
        if not content:
            continue
        title = _LECTURE_TITLE_STRIP_RE.sub("", p.title or "").strip()
        parts.append(f"### {title}\n{content[:max_chars]}")
    return "\n\n".join(parts)


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
    if _LECTURE_TITLE_RE.match(title or ""):
        # Ре-индекс: content_hash в rag_ingest пропустит источники, которые
        # фактически не изменились, пересчитает только реально новые/правленые.
        from services.rag_ingest import ingest_lecture_bg
        ingest_lecture_bg(post.id)
    return post
