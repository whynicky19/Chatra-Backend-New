"""Векторный поиск по материалам класса — заменяет client-side lecture_context
(Flutter больше не собирает и не шлёт текст лекций, см. routers/ai.py).

Два пути поиска по одному и тому же embedding-запросу:
  - Postgres + pgvector (embedding_vec, HNSW-индекс из migrations/002 и 018) —
    быстрый ANN-поиск прямо в БД;
  - Python-фолбэк (косинус по JSON-эмбеддингам в колонке `embedding`) — для
    SQLite (dev/тесты) и на случай, если pgvector-миграция ещё не накатана
    в бою. Один и тот же контракт (class_id/org_type/post_id фильтры,
    top_k, порядок по убыванию похожести) — вызывающему коду (routers/ai.py)
    без разницы, какой путь сработал.

Изоляция ВСЕГДА по class_id + org_type (defense-in-depth: основная проверка
доступа — routers/ai.py::_check_class_access, но поиск не должен утекать
между классами/организациями даже теоретически, если её обойти)."""
import json
import logging
import re
from typing import Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# top_k разный: если пользователь просит конкретную лекцию — берём почти всю
# её (лекция уже отфильтрована по post_id, там не будет "постороннего");
# свободный вопрос по классу — top-k по ВСЕМ лекциям класса, здесь k меньше,
# чтобы не раздувать промпт нерелевантными фрагментами.
TOP_K_LECTURE = 16
TOP_K_CLASS = 8

# Предохранитель на итоговый размер собранного контекста (символы) — даже
# top_k чанков большого размера не должны раздувать промпт бесконтрольно.
MAX_CONTEXT_CHARS = 12_000

_LECTURE_NUMBER_RE = re.compile(
    r"(?:лекци[юяейи]+|тем[уы]|занят(?:ии|ие|ия))\s*(?:№\s*)?(\d{1,3})"   # "лекцию 3", "лекция №5"
    r"|(\d{1,3})\s*-?(?:ой|ую|ей|ю|й)?\s*лекци[юяейи]+"                  # "2 лекцию", "10 лекции", "3-ю лекцию"
    r"|(?:lecture|lesson)\s*#?\s*(\d{1,3})",                             # "lecture 7", "lesson #4"
    re.IGNORECASE,
)


def detect_requested_lecture_number(query: str) -> Optional[int]:
    """"Объясни лекцию 3" / "объясни 2 лекцию" / "explain lecture 3" -> число.
    Русский допускает оба порядка слов ("лекцию 3" и "2 лекцию") — регексп
    учитывает оба, иначе самая естественная фраза ("объясни 2 лекцию") не
    распознавалась бы вообще, и поиск тихо уходил бы по всему классу вместо
    запрошенной лекции. None, если номер нигде явно не упомянут."""
    if not query:
        return None
    m = _LECTURE_NUMBER_RE.search(query)
    if not m:
        return None
    raw = m.group(1) or m.group(2) or m.group(3)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def resolve_lecture_post_id(db: Session, class_id: int, lecture_number: int) -> Optional[int]:
    """N-я по счёту лекция класса (то же упорядочивание, что get_all_posts:
    position ASC NULLS FIRST, затем id ASC) -> post_id. None, если такой
    лекции нет (номер вне диапазона)."""
    from crud.posts import get_all_posts
    posts = get_all_posts(db, class_id=class_id)
    if lecture_number < 1 or lecture_number > len(posts):
        return None
    return posts[lecture_number - 1].id


def _cosine_similarity(a: list, b: list) -> float:
    try:
        import numpy as np
        va = np.asarray(a, dtype="float64")
        vb = np.asarray(b, dtype="float64")
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom == 0.0:
            return 0.0
        return float(np.dot(va, vb) / denom)
    except Exception:
        return 0.0


def _search_pgvector(
    db: Session, class_id: int, org_type: str, post_id: Optional[int],
    query_vec: list, top_k: int,
):
    """Путь через pgvector. Возвращает None (не []), если СУБД не Postgres
    или колонка embedding_vec недоступна (не смигрировано) — сигнал
    вызывающему коду упасть на Python-фолбэк, а не считать, что чанков
    просто нет."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    try:
        vec_literal = "[" + ",".join(f"{x:.8f}" for x in query_vec) + "]"
        params = {"class_id": class_id, "org_type": org_type, "vec": vec_literal, "k": top_k}
        where_extra = ""
        if post_id is not None:
            where_extra = "AND post_id = :post_id"
            params["post_id"] = post_id
        rows = db.execute(sql_text(f"""
            SELECT id FROM rag_chunks
            WHERE class_id = :class_id AND org_type = :org_type {where_extra}
              AND embedding_vec IS NOT NULL
            ORDER BY embedding_vec <=> CAST(:vec AS vector)
            LIMIT :k
        """), params).fetchall()
    except Exception as e:
        logger.info("rag_search: pgvector недоступен, фолбэк на Python (%s)", e)
        db.rollback()
        return None

    ids = [r[0] for r in rows]
    if not ids:
        return []
    from models import RagChunk
    chunks = db.query(RagChunk).filter(RagChunk.id.in_(ids)).all()
    by_id = {c.id: c for c in chunks}
    return [by_id[i] for i in ids if i in by_id]


def _search_python_fallback(
    db: Session, class_id: int, org_type: str, post_id: Optional[int],
    query_vec: list, top_k: int,
) -> list:
    from models import RagChunk
    q = db.query(RagChunk).filter(RagChunk.class_id == class_id, RagChunk.org_type == org_type)
    if post_id is not None:
        q = q.filter(RagChunk.post_id == post_id)
    candidates = q.all()
    scored = []
    for c in candidates:
        try:
            vec = json.loads(c.embedding)
        except Exception:
            continue
        scored.append((_cosine_similarity(query_vec, vec), c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [c for _, c in scored[:top_k]]


async def search_class_materials(db: Session, class_id: int, org_type: str, query: str):
    """Возвращает (список RagChunk, запрошенный номер лекции|None).

    lecture_number is not None и результат пуст -> пользователь явно просил
    конкретную лекцию, которой нет (не проиндексирована/номер вне диапазона)
    — вызывающий код (routers/ai.py) обязан сказать об этом прямо, а не
    молча искать по всему классу вместо запрошенной лекции."""
    if not query or not query.strip():
        return [], None

    lecture_number = detect_requested_lecture_number(query)
    post_id = None
    if lecture_number is not None:
        post_id = resolve_lecture_post_id(db, class_id, lecture_number)
        if post_id is None:
            return [], lecture_number

    top_k = TOP_K_LECTURE if post_id is not None else TOP_K_CLASS

    from services.embeddings import embed_texts
    query_vecs = await embed_texts([query])
    if not query_vecs:
        return [], lecture_number
    query_vec = query_vecs[0]

    chunks = _search_pgvector(db, class_id, org_type, post_id, query_vec, top_k)
    if chunks is None:
        chunks = _search_python_fallback(db, class_id, org_type, post_id, query_vec, top_k)
    return chunks, lecture_number


def assemble_context(db: Session, class_id: int, chunks: list) -> str:
    """Сборка финального текстового контекста из найденных чанков: дедуп по
    id, группировка по лекции с заголовком "### Лекция N: <тема>" (тот же
    формат, что раньше строил клиент — модель уже настроена искать именно
    такие заголовки, см. routers/ai.py), порядок ВНУТРИ лекции — по
    chunk_index (порядок исходного текста, а не по релевантности — иначе
    связный конспект читался бы вперемешку). Обрезка по MAX_CONTEXT_CHARS —
    на границе ЦЕЛОГО чанка/блока лекции, никогда не посередине."""
    if not chunks:
        return ""

    seen_ids = set()
    unique = []
    for c in chunks:
        if c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        unique.append(c)

    by_post: dict = {}
    order: list = []
    for c in unique:
        if c.post_id not in by_post:
            by_post[c.post_id] = []
            order.append(c.post_id)
        by_post[c.post_id].append(c)
    for pid in by_post:
        # chunk_index нумеруется ОТДЕЛЬНО в каждом RagDocument (текст лекции
        # и каждый прикреплённый файл — разные документы, оба начинаются с
        # 0, см. services/rag_ingest.py::_embed_and_store) — сортировка
        # только по chunk_index перемешивала бы абзацы текста лекции с
        # абзацами файла, если у обоих больше одного чанка. document_id
        # сначала — чанки одного источника идут подряд, в исходном порядке;
        # порядок источников соответствует порядку инжеста (текст лекции
        # всегда первым, см. sources в rag_ingest.py::ingest_lecture).
        by_post[pid].sort(key=lambda c: (c.document_id, c.chunk_index, c.page_number or 0))

    from crud.posts import get_all_posts, _LECTURE_TITLE_STRIP_RE
    all_lecture_posts = get_all_posts(db, class_id=class_id)
    number_by_post_id = {p.id: i + 1 for i, p in enumerate(all_lecture_posts)}
    title_by_post_id = {
        p.id: _LECTURE_TITLE_STRIP_RE.sub("", p.title or "").strip() for p in all_lecture_posts
    }

    parts = []
    total_len = 0
    for pid in order:
        number = number_by_post_id.get(pid, "?")
        title = title_by_post_id.get(pid, "")
        header = f"### Лекция {number}" + (f": {title}" if title else "")
        body_text = "\n\n".join(c.text for c in by_post[pid])
        block = f"{header}\n{body_text}"
        if total_len + len(block) > MAX_CONTEXT_CHARS:
            if not parts:
                # Даже первый блок не влезает целиком — лучше обрезанный
                # контекст, чем пустой (единственная релевантная лекция).
                parts.append(block[:MAX_CONTEXT_CHARS])
            break
        parts.append(block)
        total_len += len(block)
    return "\n\n".join(parts)
