"""Обработка файлов и устойчивость ответа ИИ-грейдера к плохому вводу:
битые/пустые файлы, мусорный OCR-текст, невалидный JSON от модели."""
import asyncio
import io
import zipfile

import pytest

from services import ai_grader


# ── _parse_docx: битый/пустой .docx ─────────────────────────────────────────

def test_parse_docx_corrupted_bytes_returns_placeholder_not_raise():
    result = ai_grader._parse_docx(b"this is not a docx file at all")
    assert isinstance(result, str)
    assert "не удалось прочитать" in result


def test_parse_docx_empty_bytes_returns_placeholder_not_raise():
    result = ai_grader._parse_docx(b"")
    assert isinstance(result, str)


def test_parse_docx_valid_zip_but_not_docx_falls_back_to_raw_xml_scan():
    # ZIP валиден, но не .docx (нет word/document.xml) — второй фолбэк должен
    # либо найти пусто, либо не упасть.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "not a docx")
    result = ai_grader._parse_docx(buf.getvalue())
    assert isinstance(result, str)


# ── _looks_garbled: защита от "nnnnnnn" вместо текста ───────────────────────

def test_looks_garbled_detects_repeated_placeholder_glyph():
    assert ai_grader._looks_garbled("n" * 200) is True


def test_looks_garbled_false_for_real_text():
    assert ai_grader._looks_garbled(
        "Это обычный связный текст лекции про клеточную биологию и мембраны."
    ) is False


def test_looks_garbled_false_for_short_text():
    # Короткие строки не бракуются — иначе честные однословные ответы уходили
    # бы в "не прочитано".
    assert ai_grader._looks_garbled("ok") is False


# ── OCR-фолбэк для сканов/фото-PDF без текстового слоя ──────────────────────

def test_ocr_pdf_fallback_on_garbage_bytes_returns_empty_not_raise():
    result = asyncio.run(ai_grader._ocr_pdf_fallback(b"definitely not a pdf"))
    assert result == ""


def test_ocr_pdf_fallback_on_empty_bytes_returns_empty_not_raise():
    result = asyncio.run(ai_grader._ocr_pdf_fallback(b""))
    assert result == ""


# ── Санитайзер ответа модели: клампы и разбор confidence ────────────────────

def test_clamp_criteria_and_score_ignores_non_dict_entries():
    result = {"score": 10, "criteria_scores": ["not-a-dict", {"name": "a", "score": 3, "max": 5}]}
    ai_grader._clamp_criteria_and_score(result, max_score=100)
    assert result["score"] == 3


def test_clamp_criteria_and_score_handles_missing_score_field():
    result = {"criteria_scores": [{"name": "a", "max": 5}]}
    ai_grader._clamp_criteria_and_score(result, max_score=100)
    assert result["score"] == 0


def test_clamp_score_never_exceeds_max_score_even_without_criteria():
    result = {"score": 9999, "criteria_scores": []}
    ai_grader._clamp_criteria_and_score(result, max_score=50)
    assert result["score"] == 50


def test_parse_confidence_defaults_to_zero_on_missing_field():
    result = {}
    ai_grader._parse_confidence(result)
    assert result["confidence"] == 0
    assert result["confidence_reasons"] == []


def test_parse_confidence_clamps_out_of_range_values():
    result = {"confidence": 500, "confidence_reasons": "not-a-list"}
    ai_grader._parse_confidence(result)
    assert result["confidence"] == 100
    assert result["confidence_reasons"] == []


def test_parse_confidence_handles_non_numeric_value():
    result = {"confidence": "high"}
    ai_grader._parse_confidence(result)
    assert result["confidence"] == 0


# ── _call_openai_chat: ИИ никогда не должен отдавать "битый" результат ──────

class _RespWithContent:
    is_success = True
    status_code = 200

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}], "usage": {}}


def _fake_client_returning(content: str):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _RespWithContent(content)

    return _C


def test_call_openai_chat_strips_markdown_fences(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _fake_client_returning('```json\n{"score": 5}\n```'),
    )
    result = asyncio.run(ai_grader._call_openai_chat([]))
    assert result["score"] == 5


def test_call_openai_chat_recovers_json_embedded_in_prose(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _fake_client_returning('Конечно, вот оценка:\n{"score": 7}\nНадеюсь помог!'),
    )
    result = asyncio.run(ai_grader._call_openai_chat([]))
    assert result["score"] == 7


def test_call_openai_chat_raises_cleanly_on_unrecoverable_garbage(monkeypatch):
    """Полностью невалидный ответ (ни JSON, ни JSON в обёртке) — функция
    обязана кинуть RuntimeError с понятным сообщением, а не отдать частично
    собранный/пустой результат как будто он валиден. Роутер ловит RuntimeError
    и превращает его в 502, не давая невалидной оценке дойти до клиента."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _fake_client_returning("извините, не могу помочь с этим запросом"),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(ai_grader._call_openai_chat([]))


def test_call_openai_chat_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(ai_grader._call_openai_chat([]))


def test_call_openai_chat_raises_cleanly_when_recovered_snippet_is_still_broken(monkeypatch):
    """Регресс: раньше json.loads(match.group()) на фолбэк-пути мог кинуть
    СВОЙ json.JSONDecodeError, который не ловится `except RuntimeError` в
    роутере — наружу шёл сырой 500 вместо контролируемого 502, а сдача
    зависала в статусе "grading" навсегда."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Ровно одна '{' и одна '}' — regex r'\{.*\}' находит совпадение, но
    # незакрытая строка внутри делает сам фрагмент невалидным JSON.
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _fake_client_returning('Вот ответ: {"score": 5, "note": "не закрыто} и текст после'),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(ai_grader._call_openai_chat([]))
