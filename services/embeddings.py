"""OpenAI Embeddings API — генерация векторов для RAG (services/rag_ingest.py
при загрузке лекции, services/rag_search.py при поиске). Единственный
потребитель embedding-модели в проекте — до этой миграции RagDocument/
RagChunk существовали только как объявление таблиц, никто эмбеддинги не
считал (репетитор класса работал через client-side lecture_context).

Модель — text-embedding-3-small (1536 измерений): совпадает с EMBED_DIM в
models.py и с уже существовавшей (но неиспользуемой) pgvector-миграцией
002_rag_pgvector.sql — переиспользуем то, что уже заложено в схему, а не
придумываем новую размерность.
"""
import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Тот же паттерн ретраев, что services/ai_grader.py::_call_openai_chat —
# транзиентные 429/5xx не должны ронять весь инжест лекции из-за одного
# сбойного батча.
MAX_EMBED_RETRIES = 3
_EMBED_BACKOFF_BASE = 1.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# OpenAI разрешает до 2048 инпутов на запрос, но большой батч увеличивает
# цену повторной попытки при сбое (весь батч ретраится целиком) — режем на
# умеренные пачки.
EMBED_BATCH_SIZE = 64


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Эмбеддинги для списка текстов, в ТОМ ЖЕ порядке, что вход (OpenAI
    возвращает элементы с полем "index" — сортируем по нему явно, не
    полагаясь на порядок массива ответа). Пустой список -> пустой список.
    Кидает RuntimeError при неисправимой ошибке — вызывающий код (rag_ingest/
    rag_search) решает, что делать (пропустить документ / не сломать чат)."""
    if not texts:
        return []
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан. Добавь в .env файл.")

    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        vectors = await _embed_batch(batch, api_key)
        all_vectors.extend(vectors)
    return all_vectors


async def _embed_batch(batch: list[str], api_key: str) -> list[list[float]]:
    # Пустая строка — валидный, но бессмысленный инпут для embeddings API
    # (некоторые версии модели кидают 400) — подстраховываемся пробелом.
    payload = {"model": EMBEDDING_MODEL, "input": [t if t.strip() else " " for t in batch]}

    resp = None
    last_error = "OpenAI embeddings error"
    for attempt in range(MAX_EMBED_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    OPENAI_EMBEDDINGS_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    json=payload,
                )
        except httpx.HTTPError as e:
            last_error = f"OpenAI network error: {e}"
            resp = None
        else:
            if resp.is_success:
                break
            if resp.status_code not in _RETRYABLE_STATUS:
                break  # 4xx (кроме 429) ретраить бессмысленно
            try:
                last_error = resp.json().get("error", {}).get("message", f"OpenAI error {resp.status_code}")
            except Exception:
                last_error = f"OpenAI error {resp.status_code}"

        if attempt < MAX_EMBED_RETRIES:
            delay = _EMBED_BACKOFF_BASE * (2 ** attempt)
            if resp is not None:
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            await asyncio.sleep(delay)

    if resp is None or not resp.is_success:
        if resp is not None:
            try:
                last_error = resp.json().get("error", {}).get("message", f"OpenAI error {resp.status_code}")
            except Exception:
                last_error = f"OpenAI error {resp.status_code}"
        raise RuntimeError(last_error)

    data = resp.json()
    items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    return [item["embedding"] for item in items]
