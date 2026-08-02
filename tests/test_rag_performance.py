"""Нагрузочная проба поиска (Part 8): 10/50/100/500 "лекций" синтетических
данных на Python-фолбэке (SQLite, эта песочница — не боевой Postgres с
pgvector/HNSW). Числа здесь честные для ЭТОЙ среды и этого фолбэка, но НЕ
являются заменой реального прогона на Postgres+pgvector — см. финальный
отчёт (raw pgvector-путь в этой среде не проверялся: PostgreSQL с
расширением pgvector здесь не поднят).

Порог assert намеренно щедрый (секунды, не миллисекунды) — это дымовой тест
на явную деградацию (например случайно квадратичный алгоритм), а не
строгий perf-гейт: абсолютное время сильно зависит от машины CI."""
import json
import time

import pytest

from crud import classes as crud_classes
from crud import posts as crud_posts
from models import RagChunk, RagDocument
from services import rag_search
from tests.conftest import make_user


def _make_lecture(db, class_id, teacher, n):
    body = json.dumps({"content": f"Содержимое лекции {n}"})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] Лекция {n}", body=body, user_id=teacher.id,
    )


def _seed_lectures(db, class_id, teacher, n_lectures, chunks_per_lecture=5):
    import random
    rng = random.Random(42)
    for i in range(n_lectures):
        post = _make_lecture(db, class_id, teacher, i)
        for c in range(chunks_per_lecture):
            doc = RagDocument(
                filename="f", mime_type="text/plain", org_type="university",
                post_id=post.id, class_id=class_id,
                file_url=f"lecture-body:{post.id}:{c}", content_hash=f"h{post.id}-{c}",
            )
            db.add(doc)
            db.flush()
            vec = [rng.random() for _ in range(32)]
            db.add(RagChunk(
                document_id=doc.id, chunk_index=c,
                text=f"Лекция {i}, фрагмент {c}: " + ("текст " * 40),
                token_count=40, embedding=json.dumps(vec),
                class_id=class_id, post_id=post.id, org_type="university", source_type="text",
            ))
    db.commit()


@pytest.mark.parametrize("n_lectures", [10, 50, 100, 500])
def test_python_fallback_search_latency_at_scale(db_session, n_lectures, capsys):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, f"Perf-{n_lectures}", None, created_by=teacher.id)
    _seed_lectures(db_session, cls.id, teacher, n_lectures)

    import random
    query_vec = [random.Random(1).random() for _ in range(32)]

    start = time.perf_counter()
    results = rag_search._search_python_fallback(
        db_session, cls.id, "university", None, query_vec, top_k=rag_search.TOP_K_CLASS,
    )
    elapsed = time.perf_counter() - start

    total_chunks = n_lectures * 5
    with capsys.disabled():
        print(f"\n[perf] {n_lectures} лекций / {total_chunks} чанков: "
              f"поиск занял {elapsed * 1000:.1f} мс, найдено {len(results)}")

    assert len(results) == min(rag_search.TOP_K_CLASS, total_chunks)
    # Дымовой порог: даже 500 лекций (2500 чанков) на чистом Python — доли
    # секунды. Секунды — явный сигнал регрессии (например N+1 запрос вместо
    # одного .all()), а не просто "медленная машина".
    assert elapsed < 5.0, f"поиск по {n_lectures} лекциям занял {elapsed:.2f}с — подозрение на регрессию"


def test_assemble_context_latency_at_scale(db_session, capsys):
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Perf-assemble", None, created_by=teacher.id)
    _seed_lectures(db_session, cls.id, teacher, 100)

    chunks = db_session.query(RagChunk).filter(RagChunk.class_id == cls.id).limit(50).all()

    start = time.perf_counter()
    ctx = rag_search.assemble_context(db_session, cls.id, chunks)
    elapsed = time.perf_counter() - start

    with capsys.disabled():
        print(f"\n[perf] assemble_context на 50 чанков из 100 лекций: {elapsed * 1000:.1f} мс, "
              f"итоговый размер контекста {len(ctx)} символов")

    assert elapsed < 2.0
    assert len(ctx) <= rag_search.MAX_CONTEXT_CHARS + 1000  # с запасом на последний неполный блок


def test_ingest_lecture_query_count_does_not_grow_with_unrelated_lectures(db_session, monkeypatch):
    """N+1 проверка: инжест ОДНОЙ лекции не должен зависеть от того, сколько
    других лекций уже есть в классе (нет случайного полного скана класса)."""
    from services import ai_grader, embeddings, rag_ingest
    import asyncio

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            class _R:
                is_success = False
                status_code = 404
                content = b""
                headers = {}
            return _R()

        async def post(self, url, headers=None, json=None):
            class _R:
                is_success = True
                status_code = 200

                def json(self_inner):
                    return {"data": [{"index": i, "embedding": [0.1] * 8} for i in range(len(json["input"]))]}
            return _R()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(rag_ingest.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeClient)

    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "N1check", None, created_by=teacher.id)
    _seed_lectures(db_session, cls.id, teacher, 50)  # много "шума" в классе

    new_post = _make_lecture(db_session, cls.id, teacher, "new")

    from sqlalchemy import event
    query_count = {"n": 0}

    def _count(*a, **k):
        query_count["n"] += 1

    event.listen(db_session.bind, "before_cursor_execute", _count)
    try:
        asyncio.run(rag_ingest.ingest_lecture(db_session, new_post.id))
    finally:
        event.remove(db_session.bind, "before_cursor_execute", _count)

    # Абсолютное число запросов тут не принципиально — важно, что оно не
    # растёт линейно с количеством ДРУГИХ лекций в классе (мы их не трогаем).
    assert query_count["n"] < 30, f"инжест одной лекции сделал {query_count['n']} SQL-запросов"
