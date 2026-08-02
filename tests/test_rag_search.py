"""services/rag_search.py — векторный поиск: определение номера лекции,
изоляция по class_id/org_type (ГЛАВНОЕ требование — межклассовая утечка
недопустима ни при каких обстоятельствах), сборка контекста (дедуп,
порядок, обрезка)."""
import asyncio
import json

import pytest

from crud import classes as crud_classes
from crud import posts as crud_posts
from models import RagChunk, RagDocument
from services import rag_search
from tests.conftest import make_user


def _make_lecture(db, class_id, teacher, topic="Тема"):
    body = json.dumps({"content": f"Содержимое: {topic}"})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] {topic}", body=body, user_id=teacher.id,
    )


_chunk_counter = {"n": 0}


def _add_chunk(db, class_id, post_id, org_type, text, embedding, chunk_index=0, source_type="text"):
    _chunk_counter["n"] += 1
    unique = _chunk_counter["n"]
    doc = RagDocument(
        filename="f", mime_type="text/plain", org_type=org_type,
        post_id=post_id, class_id=class_id, file_url=f"lecture-body:{post_id}:{chunk_index}:{unique}",
        content_hash=f"hash-{post_id}-{chunk_index}-{unique}",
    )
    db.add(doc)
    db.flush()
    chunk = RagChunk(
        document_id=doc.id, chunk_index=chunk_index, text=text, token_count=len(text.split()),
        embedding=json.dumps(embedding), class_id=class_id, post_id=post_id, org_type=org_type,
        source_type=source_type,
    )
    db.add(chunk)
    db.commit()
    return chunk


# ── detect_requested_lecture_number ─────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("Объясни лекцию 3", 3),
    ("объясни 2 лекцию", 2),
    ("расскажи про лекцию №5", 5),
    ("explain lecture 7", 7),
    ("lecture #4 please", 4),
    ("Что было на 10 лекции?", 10),
    ("Что мы проходили про производные?", None),
    ("привет", None),
    ("", None),
])
def test_detect_requested_lecture_number(query, expected):
    assert rag_search.detect_requested_lecture_number(query) == expected


def test_detect_requested_lecture_number_ignores_zero_and_negative():
    assert rag_search.detect_requested_lecture_number("лекция 0") is None


# ── resolve_lecture_post_id ──────────────────────────────────────────────────

def test_resolve_lecture_post_id_maps_by_position_order(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Math", None, created_by=teacher.id)
    p1 = _make_lecture(db_session, cls.id, teacher, "Первая")
    p2 = _make_lecture(db_session, cls.id, teacher, "Вторая")
    p3 = _make_lecture(db_session, cls.id, teacher, "Третья")

    assert rag_search.resolve_lecture_post_id(db_session, cls.id, 1) == p1.id
    assert rag_search.resolve_lecture_post_id(db_session, cls.id, 2) == p2.id
    assert rag_search.resolve_lecture_post_id(db_session, cls.id, 3) == p3.id


def test_resolve_lecture_post_id_out_of_range_returns_none(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Math2", None, created_by=teacher.id)
    _make_lecture(db_session, cls.id, teacher, "Только одна")
    assert rag_search.resolve_lecture_post_id(db_session, cls.id, 5) is None
    assert rag_search.resolve_lecture_post_id(db_session, cls.id, 0) is None


# ── Изоляция: НИКОГДА не утекать между классами/организациями ──────────────

def test_search_never_leaks_across_classes(db_session):
    """Критический security-тест: две лекции в РАЗНЫХ классах с ПОЧТИ
    одинаковым embedding-вектором — поиск в классе A не должен вернуть
    чанк класса B, даже если он ближе по косинусу."""
    teacher = make_user(db_session, role="teacher")
    cls_a = crud_classes.create_class(db_session, "Class A", None, created_by=teacher.id)
    cls_b = crud_classes.create_class(db_session, "Class B", None, created_by=teacher.id)
    post_a = _make_lecture(db_session, cls_a.id, teacher, "A")
    post_b = _make_lecture(db_session, cls_b.id, teacher, "B")

    query_vec = [1.0, 0.0, 0.0, 0.0]
    _add_chunk(db_session, cls_a.id, post_a.id, "university", "Материал класса A", [0.9, 0.1, 0.0, 0.0])
    # Класс B специально ДАЖЕ БЛИЖЕ к запросу, чем класс A — если изоляция
    # сломана, поиск в классе A вернул бы именно этот чанк.
    _add_chunk(db_session, cls_b.id, post_b.id, "university", "Материал класса B (секретный)", [1.0, 0.0, 0.0, 0.0])

    results = rag_search._search_python_fallback(
        db_session, cls_a.id, "university", None, query_vec, top_k=10,
    )
    texts = [c.text for c in results]
    assert "Материал класса A" in texts
    assert "Материал класса B (секретный)" not in texts
    assert all(c.class_id == cls_a.id for c in results)


def test_search_never_leaks_across_organizations(db_session):
    """Тот же class_id теоретически не должен встречаться в двух org_type
    одновременно на практике, но проверяем defense-in-depth фильтр явно."""
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Shared id space", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, "T")

    query_vec = [1.0, 0.0]
    _add_chunk(db_session, cls.id, post.id, "university", "Материал university", [1.0, 0.0])
    _add_chunk(db_session, cls.id, post.id, "school", "Материал school", [1.0, 0.0])

    results = rag_search._search_python_fallback(db_session, cls.id, "university", None, query_vec, top_k=10)
    texts = [c.text for c in results]
    assert "Материал university" in texts
    assert "Материал school" not in texts


def test_search_scoped_to_lecture_ignores_closer_chunk_in_other_lecture(db_session):
    """"Объясни лекцию 3" — поиск ДОЛЖЕН искать только внутри лекции 3, даже
    если в лекции 5 есть чанк семантически ближе к вопросу."""
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Scoped", None, created_by=teacher.id)
    post3 = _make_lecture(db_session, cls.id, teacher, "Лекция про производные")
    post5 = _make_lecture(db_session, cls.id, teacher, "Другая лекция")

    query_vec = [1.0, 0.0]
    _add_chunk(db_session, cls.id, post3.id, "university", "Материал лекции 3 (менее похож)", [0.6, 0.4])
    _add_chunk(db_session, cls.id, post5.id, "university", "Материал лекции 5 (более похож)", [1.0, 0.0])

    results = rag_search._search_python_fallback(
        db_session, cls.id, "university", post3.id, query_vec, top_k=10,
    )
    assert len(results) == 1
    assert results[0].text == "Материал лекции 3 (менее похож)"


def test_search_class_materials_end_to_end_scopes_by_lecture_number(db_session, monkeypatch):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "E2E", None, created_by=teacher.id)
    post1 = _make_lecture(db_session, cls.id, teacher, "Первая")
    post2 = _make_lecture(db_session, cls.id, teacher, "Вторая")
    _add_chunk(db_session, cls.id, post1.id, "university", "Текст первой лекции", [1.0, 0.0])
    _add_chunk(db_session, cls.id, post2.id, "university", "Текст второй лекции", [1.0, 0.0])

    async def _fake_embed_texts(texts):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(rag_search, "embed_texts", _fake_embed_texts, raising=False)
    import services.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module, "embed_texts", _fake_embed_texts)

    chunks, lecture_number = asyncio.run(
        rag_search.search_class_materials(db_session, cls.id, "university", "объясни лекцию 2")
    )
    assert lecture_number == 2
    assert len(chunks) == 1
    assert chunks[0].post_id == post2.id


def test_search_class_materials_requested_lecture_not_found_returns_empty_not_fallback(db_session, monkeypatch):
    """Явно попросили лекцию, которой нет — НЕ должны молча искать по всему
    классу вместо неё (иначе модель ответит не про то, о чём спросили)."""
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "OneLecture", None, created_by=teacher.id)
    post1 = _make_lecture(db_session, cls.id, teacher, "Единственная")
    _add_chunk(db_session, cls.id, post1.id, "university", "Текст", [1.0, 0.0])

    chunks, lecture_number = asyncio.run(
        rag_search.search_class_materials(db_session, cls.id, "university", "объясни лекцию 99")
    )
    assert lecture_number == 99
    assert chunks == []


def test_search_class_materials_empty_query_returns_nothing(db_session):
    chunks, lecture_number = asyncio.run(
        rag_search.search_class_materials(db_session, 1, "university", "")
    )
    assert chunks == []
    assert lecture_number is None


# ── assemble_context: дедуп, порядок, обрезка ────────────────────────────────

def test_assemble_context_empty_chunks_returns_empty_string(db_session):
    assert rag_search.assemble_context(db_session, 1, []) == ""


def test_assemble_context_dedups_by_id(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Dedup", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, "Тема")
    chunk = _add_chunk(db_session, cls.id, post.id, "university", "Уникальный текст", [1.0, 0.0])

    ctx = rag_search.assemble_context(db_session, cls.id, [chunk, chunk, chunk])
    assert ctx.count("Уникальный текст") == 1


def test_assemble_context_preserves_chunk_order_within_lecture(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Order", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, "Тема")
    c2 = _add_chunk(db_session, cls.id, post.id, "university", "Второй фрагмент", [1.0, 0.0], chunk_index=2)
    c0 = _add_chunk(db_session, cls.id, post.id, "university", "Первый фрагмент", [1.0, 0.0], chunk_index=0)
    c1 = _add_chunk(db_session, cls.id, post.id, "university", "Средний фрагмент", [1.0, 0.0], chunk_index=1)

    # Передаём чанки НЕ по порядку (как если бы поиск вернул их по релевантности)
    ctx = rag_search.assemble_context(db_session, cls.id, [c2, c0, c1])
    assert ctx.index("Первый фрагмент") < ctx.index("Средний фрагмент") < ctx.index("Второй фрагмент")


def test_assemble_context_labels_lecture_number_and_title(db_session):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Labels", None, created_by=teacher.id)
    post1 = _make_lecture(db_session, cls.id, teacher, "Векторы")
    post2 = _make_lecture(db_session, cls.id, teacher, "Матрицы")
    c1 = _add_chunk(db_session, cls.id, post1.id, "university", "Про векторы", [1.0, 0.0])
    c2 = _add_chunk(db_session, cls.id, post2.id, "university", "Про матрицы", [1.0, 0.0])

    ctx = rag_search.assemble_context(db_session, cls.id, [c1, c2])
    assert "### Лекция 1: Векторы" in ctx
    assert "### Лекция 2: Матрицы" in ctx


def test_assemble_context_truncates_at_char_budget_boundary(db_session, monkeypatch):
    monkeypatch.setattr(rag_search, "MAX_CONTEXT_CHARS", 200)
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Truncate", None, created_by=teacher.id)
    chunks = []
    for i in range(10):
        post = _make_lecture(db_session, cls.id, teacher, f"Лекция {i}")
        chunks.append(_add_chunk(db_session, cls.id, post.id, "university", "x" * 150, [1.0, 0.0]))

    ctx = rag_search.assemble_context(db_session, cls.id, chunks)
    assert len(ctx) <= 200 + 50  # с запасом на заголовки блоков
