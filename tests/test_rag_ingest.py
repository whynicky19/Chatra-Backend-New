"""services/rag_ingest.py — инжест лекции в RAG-хранилище: dedup по
content_hash, регенерация при правке, удаление пропавших источников,
изоляция по классу/лекции, устойчивость к сбоям одного источника.

ingest_lecture_bg — no-op под pytest (см. сам модуль): фоновый поток со
своим event loop'ом и своим соединением к БД реально стрелял в OpenAI и
ловил "database is locked" на файловой SQLite тестовой БД, гоняясь с
соединением самого теста. Поэтому все тесты вызывают ingest_lecture()
НАПРЯМУЮ (синхронно через asyncio.run, с замоканным httpx)."""
import asyncio
import io
import json

import pytest
from PIL import Image

from crud import classes as crud_classes
from crud import posts as crud_posts
from models import RagChunk, RagDocument
from services import ai_grader, embeddings, rag_ingest
from tests.conftest import make_user


def _png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color=color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, content=b"", content_type="text/plain", status_code=200):
        self.content = content
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.headers = {"content-type": content_type}

    def json(self):
        return self._json_payload


class _FakeIngestClient:
    """Единый fake httpx.AsyncClient: GET отдаёт содержимое по URL (настроено
    через file_map), POST — эмбеддинги/vision-подпись по URL эндпоинта.
    Один и тот же класс монтируется в services.rag_ingest.httpx,
    services.embeddings.httpx и services.ai_grader.httpx — все три модуля
    импортируют httpx независимо."""
    file_map: dict = {}
    caption_text = "Подпись картинки для индексации."
    post_calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **k):
        base = url.split("?")[0]
        for key, (content, ctype) in _FakeIngestClient.file_map.items():
            if key in base:
                return _FakeResp(content=content, content_type=ctype)
        return _FakeResp(content=b"", content_type="text/plain", status_code=404)

    async def post(self, url, headers=None, json=None):
        _FakeIngestClient.post_calls.append(url)
        if "embeddings" in url:
            inputs = json["input"]
            resp = _FakeResp()
            resp._json_payload = {
                "data": [{"index": i, "embedding": _fake_vector(t)} for i, t in enumerate(inputs)]
            }
            return resp
        resp = _FakeResp()
        resp._json_payload = {"choices": [{"message": {"content": _FakeIngestClient.caption_text}}]}
        return resp


def _fake_vector(text: str) -> list:
    import hashlib
    h = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in h[:8]]


def _reset_fake_client():
    _FakeIngestClient.file_map = {}
    _FakeIngestClient.post_calls = []


def _patch_httpx(monkeypatch):
    monkeypatch.setattr(rag_ingest.httpx, "AsyncClient", _FakeIngestClient)
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeIngestClient)
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeIngestClient)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _make_lecture(db, class_id, teacher, topic="Тема", content="Содержимое лекции.", files=None):
    body = json.dumps({"content": content, **({"files": files} if files else {})})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] {topic}", body=body, user_id=teacher.id,
    )


# ── Базовый инжест ───────────────────────────────────────────────────────────

def test_ingest_lecture_creates_document_and_chunks_for_body_text(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Math", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="Производная функции — предел отношения приращений.")

    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    docs = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    assert len(docs) == 1
    assert docs[0].class_id == cls.id
    assert docs[0].org_type == "university"

    chunks = db_session.query(RagChunk).filter(RagChunk.document_id == docs[0].id).all()
    assert len(chunks) >= 1
    assert chunks[0].class_id == cls.id
    assert chunks[0].post_id == post.id
    assert chunks[0].source_type == "text"
    assert "Производная" in chunks[0].text


def test_ingest_lecture_skips_non_lecture_posts(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    post = crud_posts.create_new_post(
        db_session, title="Обычный пост, не лекция", body=json.dumps({"content": "текст"}),
        user_id=teacher.id,
    )
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    assert db_session.query(RagDocument).filter(RagDocument.post_id == post.id).count() == 0


def test_ingest_lecture_missing_post_is_noop(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    asyncio.run(rag_ingest.ingest_lecture(db_session, 999999))  # не должно упасть


def test_ingest_lecture_empty_body_creates_no_document(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Empty", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="")
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    assert db_session.query(RagDocument).filter(RagDocument.post_id == post.id).count() == 0


# ── Dedup / идемпотентность / регенерация ────────────────────────────────────

def test_ingest_lecture_unchanged_content_skips_reembedding(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Physics", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="Закон сохранения энергии.")

    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    first_doc_id = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).one().id
    calls_after_first = len(_FakeIngestClient.post_calls)
    assert calls_after_first > 0

    # Повторный инжест БЕЗ изменений — не должен звать embeddings API снова.
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    assert len(_FakeIngestClient.post_calls) == calls_after_first

    doc = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).one()
    assert doc.id == first_doc_id


def test_ingest_lecture_changed_content_regenerates_embeddings(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Chem", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="pH меньше 7 — кислота.")
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    crud_posts.update_post(
        db_session, post.id, post.title,
        json.dumps({"content": "pH больше 7 — щёлочь. Совсем другой текст."}),
    )
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    docs = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    assert len(docs) == 1  # старый заменён, не задвоен
    chunks = db_session.query(RagChunk).filter(RagChunk.document_id == docs[0].id).all()
    assert any("щёлочь" in c.text for c in chunks)
    assert not any("кислота" in c.text for c in chunks)


def test_ingest_lecture_removed_file_cleans_up_stale_document(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    _FakeIngestClient.file_map["file1.txt"] = (b"Text of file one.", "text/plain")
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Bio", None, created_by=teacher.id)
    file_url = "http://localhost:8000/api/uploads/file1.txt"
    post = _make_lecture(db_session, cls.id, teacher, content="Текст.", files=[file_url])
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    assert db_session.query(RagDocument).filter(RagDocument.post_id == post.id).count() == 2  # body + file

    # Убираем файл из лекции — его RagDocument должен исчезнуть.
    crud_posts.update_post(db_session, post.id, post.title, json.dumps({"content": "Текст."}))
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    docs = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    assert len(docs) == 1
    assert docs[0].file_url.startswith("lecture-body:")


# ── Удаление лекции каскадом ────────────────────────────────────────────────

def test_delete_lecture_cascades_to_rag_documents_and_chunks(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Hist", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="Текст лекции по истории.")
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    doc = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).one()
    chunk_ids = [c.id for c in db_session.query(RagChunk).filter(RagChunk.document_id == doc.id).all()]
    assert chunk_ids

    crud_posts.delete_post(db_session, post.id)

    assert db_session.query(RagDocument).filter(RagDocument.post_id == post.id).count() == 0
    assert db_session.query(RagChunk).filter(RagChunk.id.in_(chunk_ids)).count() == 0


# ── Изображения (встроенные и отдельные) — vision-подпись ──────────────────

def test_ingest_lecture_attached_image_gets_vision_caption(db_session, monkeypatch):
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    _FakeIngestClient.file_map["photo.jpg"] = (_png_bytes(), "image/jpeg")
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Art", None, created_by=teacher.id)
    file_url = "http://localhost:8000/api/uploads/photo.jpg"
    post = _make_lecture(db_session, cls.id, teacher, content="", files=[file_url])

    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    docs = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    assert len(docs) == 1  # только файл, body пуст
    chunks = db_session.query(RagChunk).filter(RagChunk.document_id == docs[0].id).all()
    assert len(chunks) == 1
    assert chunks[0].source_type == "image_caption"
    assert chunks[0].text == _FakeIngestClient.caption_text


# ── Устойчивость к сбою одного источника ────────────────────────────────────

def test_ingest_lecture_one_failing_source_does_not_undo_others(db_session, monkeypatch):
    """Регресс: раньше вся лекция обрабатывалась в ОДНОЙ транзакции — сбой на
    втором источнике (например файле) откатывал db.rollback()'ом уже
    успешно обработанный и ЗАКОММИЧЕННЫЙ первый источник (текст лекции) той
    же лекции, потому что оба сидели в одной незакоммиченной транзакции.

    Лекция здесь имеет ДВА источника: текст лекции (обрабатывается первым,
    должен успешно закоммититься) и файл (обрабатывается вторым, эмбеддинг
    для него намеренно ломается). Проверяем, что первый источник survives."""
    _patch_httpx(monkeypatch)
    _reset_fake_client()
    _FakeIngestClient.file_map["file1.txt"] = (b"Text of attached file.", "text/plain")
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "Geo", None, created_by=teacher.id)
    file_url = "http://localhost:8000/api/uploads/file1.txt"
    post = _make_lecture(
        db_session, cls.id, teacher,
        content="Текст лекции, который должен уцелеть.", files=[file_url],
    )

    import services.embeddings as embeddings_module
    real_embed_texts = embeddings_module.embed_texts
    call_count = {"n": 0}

    async def _flaky_embed_texts(texts):
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1-й вызов — текст лекции (успех), 2-й — файл (сбой)
            raise RuntimeError("симулированный сетевой сбой на втором источнике")
        return await real_embed_texts(texts)

    monkeypatch.setattr(embeddings_module, "embed_texts", _flaky_embed_texts)

    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))

    docs = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    # Источник текста лекции должен быть закоммичен, несмотря на сбой файла.
    body_docs = [d for d in docs if d.file_url.startswith("lecture-body:")]
    file_docs = [d for d in docs if d.file_url == file_url]
    assert len(body_docs) == 1, "текст лекции должен уцелеть после сбоя на файле"
    assert len(file_docs) == 0, "файл с упавшим эмбеддингом не должен создать документ"

    chunks = db_session.query(RagChunk).filter(RagChunk.document_id == body_docs[0].id).all()
    assert any("уцелеть" in c.text for c in chunks)

    # Сессия не должна остаться в битом состоянии — повторный инжест
    # (например следующий update_post) подхватывает пропущенный файл.
    monkeypatch.setattr(embeddings_module, "embed_texts", real_embed_texts)
    asyncio.run(rag_ingest.ingest_lecture(db_session, post.id))
    docs2 = db_session.query(RagDocument).filter(RagDocument.post_id == post.id).all()
    assert len(docs2) == 2


# ── ingest_lecture_bg — no-op под pytest ────────────────────────────────────

def test_ingest_lecture_bg_is_noop_under_pytest(db_session, monkeypatch):
    """create_new_post вызывает ingest_lecture_bg — под pytest он должен
    ничего не делать (см. docstring модуля): иначе фоновый поток стрелял бы
    настоящими запросами в OpenAI и ловил гонки на файловой SQLite."""
    teacher = make_user(db_session, role="teacher")
    cls = crud_classes.create_class(db_session, "NoOp", None, created_by=teacher.id)
    post = _make_lecture(db_session, cls.id, teacher, content="Текст.")
    # Никакого мока httpx не ставим — если бы ingest_lecture_bg реально
    # запустился, тест либо завис/упал бы на реальном сетевом вызове, либо
    # (что и произошло при первой версии этого кода) поймал бы "database is
    # locked" на параллельном потоке.
    import time
    time.sleep(0.05)
    assert db_session.query(RagDocument).filter(RagDocument.post_id == post.id).count() == 0
