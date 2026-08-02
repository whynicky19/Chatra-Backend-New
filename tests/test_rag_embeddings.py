"""services/embeddings.py — обёртка OpenAI Embeddings API: батчинг, ретраи,
сохранение порядка вход->выход."""
import asyncio

import pytest

from services import embeddings


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


def _embedding_payload(inputs):
    # OpenAI отдаёт элементы с полем "index" — в ответе НЕ обязательно по
    # порядку входа; тест намеренно переставляет их местами.
    items = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(inputs))]
    return {"data": list(reversed(items))}


class _FakeClient:
    calls = 0
    fail_times = 0
    status_sequence = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls += 1
        if _FakeClient.status_sequence:
            code = _FakeClient.status_sequence.pop(0)
            if code != 200:
                return _FakeResp(status_code=code, payload={"error": {"message": f"err {code}"}})
        return _FakeResp(status_code=200, payload=_embedding_payload(json["input"]))


def _reset_fake_client():
    _FakeClient.calls = 0
    _FakeClient.status_sequence = None


def test_embed_texts_empty_list_returns_empty():
    assert asyncio.run(embeddings.embed_texts([])) == []


def test_embed_texts_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(embeddings.embed_texts(["текст"]))


def test_embed_texts_preserves_input_order_despite_unordered_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_fake_client()
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    vectors = asyncio.run(embeddings.embed_texts(["a", "b", "c"]))
    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]


def test_embed_texts_batches_large_input(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_fake_client()
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    texts = [f"text-{i}" for i in range(embeddings.EMBED_BATCH_SIZE * 2 + 5)]
    vectors = asyncio.run(embeddings.embed_texts(texts))
    assert len(vectors) == len(texts)
    assert _FakeClient.calls == 3  # 2 полных батча + 1 маленький


def test_embed_texts_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_fake_client()
    _FakeClient.status_sequence = [429, 200]
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(embeddings.asyncio, "sleep", _fast_sleep)
    vectors = asyncio.run(embeddings.embed_texts(["a"]))
    assert vectors == [[0.0] * 4]
    assert _FakeClient.calls == 2


async def _fast_sleep(_seconds):
    return None


def test_embed_texts_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_fake_client()
    _FakeClient.status_sequence = [500] * (embeddings.MAX_EMBED_RETRIES + 5)
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(embeddings.asyncio, "sleep", _fast_sleep)
    with pytest.raises(RuntimeError):
        asyncio.run(embeddings.embed_texts(["a"]))


def test_embed_texts_does_not_retry_non_retryable_4xx(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_fake_client()
    _FakeClient.status_sequence = [400]
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(embeddings.asyncio, "sleep", _fast_sleep)
    with pytest.raises(RuntimeError):
        asyncio.run(embeddings.embed_texts(["a"]))
    assert _FakeClient.calls == 1  # без ретраев на 400
