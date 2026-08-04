"""Инжест лекции в RAG-хранилище (RagDocument/RagChunk) — заменяет
client-side lecture_context (Flutter собирал текст всех лекций и слал в
каждый /ai/chat). Теперь материалы индексируются ОДИН РАЗ при создании/
редактировании лекции, а услуга поиска (services/rag_search.py) достаёт
только релевантные чанки под конкретный вопрос.

Источники одной лекции:
  - "текст лекции" — content, который учитель напечатал прямо в форме;
  - каждый прикреплённый файл (docx/pdf/pptx/txt/картинка/HEIC — тот же
    парсинг, что и в services/ai_grader.py: OCR-фолбэк для сканов, встроенные
    картинки docx/pptx, страницы скан-PDF как vision-изображения).

Изображения (встроенные и отдельные) не эмбеддятся напрямую — у OpenAI нет
мультимодальной embedding-модели в этом стеке, поэтому получаем текстовую
подпись/транскрипцию через vision (тот же GPT-4o, что и грейдинг) и
эмбеддим её — так диаграмма/формула/скан становятся семантически искомыми.

Идемпотентность: dedup по content_hash (SHA-256 контента источника) — если
файл/текст не изменился с прошлого инжеста, эмбеддинги не пересчитываются
(экономит деньги и время). Источники, пропавшие из лекции (файл убрали),
подчищаются.
"""
import asyncio
import hashlib
import json
import logging
import threading

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
CAPTION_MODEL = "gpt-4o"

# Изображений на файл, которые реально подписываются vision'ом — конспект с
# 40 фото был бы и дорогим, и медленным инжестом одной лекции.
MAX_CAPTION_IMAGES_PER_SOURCE = 8

_CAPTION_SYSTEM_PROMPT = (
    "Ты готовишь материалы лекции для базы знаний, по которой потом ищут "
    "ответы. Опиши это изображение текстом: если это текст/конспект — "
    "перепиши его дословно; если формула — запиши в LaTeX; если диаграмма, "
    "график или таблица — опиши структуру, подписи и числа. Только "
    "содержание, без вступительных фраз вроде \"На изображении показано\"."
)


async def _caption_image(data_uri: str) -> str:
    """Vision-подпись картинки для индексации. Пустая строка при любой
    ошибке — вызывающий код просто не создаёт чанк для этой картинки,
    инжест лекции в целом не должен падать из-за одной нечитаемой картинки."""
    import os
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return ""
    payload = {
        "model": CAPTION_MODEL,
        "messages": [
            {"role": "system", "content": _CAPTION_SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri}}]},
        ],
        "max_tokens": 800,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                OPENAI_URL,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                json=payload,
            )
        if not resp.is_success:
            return ""
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.warning("rag_ingest: не удалось подписать изображение (%s)", e)
        return ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _ChunkDraft:
    """Промежуточное представление чанка ДО эмбеддинга — text/page_number/
    source_type уже известны, embedding проставляется одним батчем в конце
    (см. _embed_and_store), чтобы не звать API по одному чанку за раз."""

    __slots__ = ("text", "page_number", "source_type")

    def __init__(self, text: str, page_number: int | None, source_type: str):
        self.text = text
        self.page_number = page_number
        self.source_type = source_type


async def _build_chunks_for_text(text: str, source_type: str = "text") -> list[_ChunkDraft]:
    from services.chunking import chunk_text
    pieces = chunk_text(text)
    return [_ChunkDraft(p, None, source_type) for p in pieces]


async def _build_chunks_for_lecture_body(content: str) -> list[_ChunkDraft]:
    return await _build_chunks_for_text(content, source_type="text")


async def _build_chunks_for_file(url: str) -> tuple[list[_ChunkDraft], bytes]:
    """Возвращает (чанки, сырые байты файла для content_hash). Переиспользует
    приватные функции ai_grader — тот же пайплайн extraction/OCR, что и
    грейдинг: одна кодовая база для "прочитать файл" везде в проекте."""
    from services.ai_grader import (
        _fetch_file_text, _fetch_embedded_images, _fetch_pdf_page_images,
        _fetch_image_data_uri, IMAGE_EXTS,
    )
    from services.url_safety import is_safe_fetch_url
    from services.file_urls import sign_upload_url

    ext = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url.split("?")[0] else ""

    raw_bytes = b""
    if is_safe_fetch_url(url):
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.get(sign_upload_url(url))
                if resp.is_success:
                    raw_bytes = resp.content
        except Exception:
            raw_bytes = b""

    chunks: list[_ChunkDraft] = []

    if ext in IMAGE_EXTS:
        # Отдельно прикреплённое фото (скриншот/скан/фото конспекта) — не
        # текстовый файл вообще, единственный источник контента — vision.
        data_uri = await _fetch_image_data_uri(url)
        if data_uri:
            caption = await _caption_image(data_uri)
            if caption.strip():
                chunks.extend(await _build_chunks_for_text(caption, source_type="image_caption"))
        return chunks, raw_bytes

    file_text = await _fetch_file_text(url)
    if file_text.strip() and not file_text.startswith("["):
        chunks.extend(await _build_chunks_for_text(file_text, source_type="text"))

    embedded_images = await _fetch_embedded_images(url)      # docx/pptx
    embedded_images += await _fetch_pdf_page_images(url)     # сканы без текстового слоя
    for data_uri in embedded_images[:MAX_CAPTION_IMAGES_PER_SOURCE]:
        caption = await _caption_image(data_uri)
        if caption.strip():
            chunks.extend(await _build_chunks_for_text(caption, source_type="image_caption"))

    return chunks, raw_bytes


def _parse_lecture_files(body_json: str) -> list[str]:
    try:
        body = json.loads(body_json)
    except Exception:
        return []
    files = body.get("files")
    return [f for f in files if isinstance(f, str) and f.strip()] if isinstance(files, list) else []


def _parse_lecture_content(body_json: str) -> str:
    try:
        body = json.loads(body_json)
        return (body.get("content") or body.get("description") or "").strip()
    except Exception:
        return (body_json or "").strip()


async def _embed_and_store(
    db: Session, document_id: int, drafts: list[_ChunkDraft],
    class_id: int, post_id: int, org_type: str,
) -> None:
    from services.embeddings import embed_texts
    from services.chunking import count_tokens
    from models import RagChunk

    if not drafts:
        return
    vectors = await embed_texts([d.text for d in drafts])
    for i, (draft, vector) in enumerate(zip(drafts, vectors)):
        db.add(RagChunk(
            document_id=document_id,
            chunk_index=i,
            text=draft.text,
            token_count=count_tokens(draft.text),
            embedding=json.dumps(vector),
            page_number=draft.page_number,
            source_type=draft.source_type,
            class_id=class_id,
            post_id=post_id,
            org_type=org_type,
        ))

    # embedding_vec (VECTOR(1536), вне ORM-модели — см. models.py:RagChunk) не
    # заполняется через ORM insert выше, поэтому дублируем его отдельным
    # UPDATE сразу после вставки — иначе services/rag_search.py::_search_pgvector
    # фильтрует "WHERE embedding_vec IS NOT NULL" и молча не находит ни одного
    # свежего чанка (а Python-фолбэк не срабатывает, т.к. пустой список — не
    # сигнал "pgvector недоступен", это валидный ответ "ничего не нашлось").
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.flush()
        db.execute(sql_text(
            "UPDATE rag_chunks SET embedding_vec = embedding::vector "
            "WHERE document_id = :doc_id AND embedding_vec IS NULL"
        ), {"doc_id": document_id})


async def ingest_lecture(db: Session, post_id: int) -> None:
    """Полный (пере)инжест одной лекции. Идемпотентно: безопасно вызывать
    повторно (create/update лекции, ручной ре-индекс) — не изменившиеся
    источники пропускаются (content_hash), пропавшие — удаляются.

    Каждый источник коммитится ОТДЕЛЬНО (а не одной транзакцией на всю
    лекцию): иначе сбой на одном файле (например сетевая ошибка на файле #2)
    откатывал бы db.rollback()'ом уже успешно обработанный и закоммиченный
    файл #1 в той же транзакции. Конкурентные правки лекции не страшны и без
    межпроцессной блокировки: каждый update_post запускает свой
    ingest_lecture_bg, и результат всегда сходится к последней успешно
    завершившейся обработке — то есть к самой свежей правке."""
    from models import Posts, RagDocument
    from crud.posts import _LECTURE_TITLE_RE

    post = db.query(Posts).filter(Posts.id == post_id).first()
    if not post:
        return
    m = _LECTURE_TITLE_RE.match(post.title or "")
    if not m:
        return  # не лекция (или заголовок сменили так, что перестала ей быть)
    class_id = int(m.group(1))

    from models import User
    creator = db.query(User).filter(User.id == post.user_id).first()
    org_type = creator.org_type if creator else "university"

    lecture_content = _parse_lecture_content(post.body)
    file_urls = _parse_lecture_files(post.body)

    # (source_key, extractor) — source_key синтетический для текста лекции
    # (NOT NULL + участвует в UniqueConstraint(post_id, file_url)).
    sources: list[tuple[str, str]] = [(f"lecture-body:{post_id}", "body")]
    sources += [(url, "file") for url in file_urls]

    existing_docs = {
        d.file_url: d
        for d in db.query(RagDocument).filter(RagDocument.post_id == post_id).all()
    }
    keep_ids: set[int] = set()

    for source_key, kind in sources:
        try:
            existing = existing_docs.get(source_key)

            if kind == "body":
                if not lecture_content:
                    continue
                content_hash = _sha256(lecture_content.encode("utf-8"))
                if existing and existing.content_hash == content_hash:
                    keep_ids.add(existing.id)
                    continue
                drafts = await _build_chunks_for_lecture_body(lecture_content)
                filename = "Текст лекции"
                mime_type = "text/plain"
            else:
                drafts, raw_bytes = await _build_chunks_for_file(source_key)
                content_hash = _sha256(raw_bytes) if raw_bytes else _sha256(source_key.encode("utf-8"))
                if existing and existing.content_hash == content_hash:
                    keep_ids.add(existing.id)
                    continue
                filename = source_key.rsplit("#", 1)[-1] if "#" in source_key else source_key.rsplit("/", 1)[-1]
                ext = source_key.split("?")[0].rsplit(".", 1)[-1].lower() if "." in source_key.split("?")[0] else ""
                mime_type = ext or "application/octet-stream"

            if existing:
                db.delete(existing)
                db.flush()

            doc = RagDocument(
                filename=filename, mime_type=mime_type, org_type=org_type,
                post_id=post_id, class_id=class_id, file_url=source_key,
                content_hash=content_hash,
            )
            db.add(doc)
            try:
                db.flush()
            except IntegrityError:
                # Конкурентный инжест той же лекции уже создал эту строку —
                # не наш случай выигрывать гонку, откатываем и идём дальше;
                # финальное состояние всё равно сойдётся к последней правке
                # (каждый update_post запускает свежий ingest_lecture_bg).
                db.rollback()
                continue

            await _embed_and_store(db, doc.id, drafts, class_id, post_id, org_type)
            db.commit()
            keep_ids.add(doc.id)
        except Exception as e:
            logger.warning("rag_ingest: источник %s лекции %s пропущен (%s)", source_key, post_id, e)
            db.rollback()
            continue

    # Источники, пропавшие из лекции (файл отвязали/удалили) — подчищаем
    # отдельным коммитом, чтобы сбой здесь не затронул уже обработанное выше.
    stale = [doc for doc in existing_docs.values() if doc.id not in keep_ids]
    if stale:
        try:
            for doc in stale:
                db.delete(doc)
            db.commit()
        except Exception as e:
            logger.warning("rag_ingest: не удалось удалить устаревшие источники лекции %s (%s)", post_id, e)
            db.rollback()


def ingest_lecture_bg(post_id: int) -> None:
    """Fire-and-forget запуск инжеста в отдельном потоке со своим event loop
    и своей сессией БД — тот же паттерн, что services/fcm.py::send_push_bg.
    Вызывается из crud/posts.py при создании/правке лекции; не блокирует
    HTTP-ответ пользователю (эмбеддинги/vision-подписи — секунды на файл).

    Под pytest — no-op. Раньше это отсутствовало: любой тест, создающий
    лекцию через crud_posts.create_new_post/update_post (даже тесты,
    вообще не имеющие отношения к RAG — например про grading lecture_context),
    тихо запускал фоновый поток, который бил РЕАЛЬНЫМИ запросами в OpenAI
    (main.py::load_dotenv() подтягивает настоящий .env с боевым ключом) и
    писал в ту же файловую SQLite тестовой БД ИЗ ДРУГОГО ПОТОКА — поймано
    "database is locked" из-за гонки с соединением самого теста. Тесты,
    которые целенаправленно проверяют инжест, вызывают ingest_lecture()
    напрямую (синхронно, с замоканным httpx) — см. tests/test_rag_ingest.py."""
    import os
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    def _worker():
        from db import SessionLocal
        db = SessionLocal()
        try:
            asyncio.run(ingest_lecture(db, post_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest_lecture_bg: сбой инжеста лекции %s (%s)", post_id, e)
        finally:
            db.close()

    threading.Thread(target=_worker, daemon=True).start()
