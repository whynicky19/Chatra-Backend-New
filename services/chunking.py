"""Разбиение текста материала лекции на чанки для RAG (services/rag_ingest.py).

tiktoken уже был в requirements.txt (нигде не использовался) — переиспользуем
его для честного подсчёта токенов вместо приближения по словам/символам,
раз уж зависимость всё равно тянется в образ.
"""
from typing import Optional

try:
    import tiktoken
except Exception:  # pragma: no cover - tiktoken всегда должен быть доступен
    tiktoken = None

_ENCODING = None
_ENCODING_LOAD_FAILED = False


def _encoding():
    """cl100k_base — энкодер моделей gpt-4o/embeddings. Скачивает таблицу
    мержей при первом вызове (tiktoken кэширует на диск) — если сети нет
    (офлайн CI), тихо переключаемся на фолбэк по словам, а не падаем."""
    global _ENCODING, _ENCODING_LOAD_FAILED
    if _ENCODING is not None:
        return _ENCODING
    if _ENCODING_LOAD_FAILED or tiktoken is None:
        return None
    try:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
        return _ENCODING
    except Exception:
        _ENCODING_LOAD_FAILED = True
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoding()
    if enc is not None:
        return len(enc.encode(text))
    # Грубый фолбэк — не даёт точного числа, но не роняет пайплайн.
    return max(1, len(text.split()))


# ~400 токенов на чанк — достаточно контекста для связного ответа модели по
# одному фрагменту, но не настолько много, чтобы top-k=8-12 чанков раздули
# промпт (см. services/rag_search.py::MAX_CONTEXT_CHARS). Overlap 60 токенов
# — вопрос на границе двух абзацев не теряет контекст соседнего чанка.
CHUNK_TOKEN_SIZE = 400
CHUNK_TOKEN_OVERLAP = 60


def chunk_text(
    text: str, chunk_size: int = CHUNK_TOKEN_SIZE, overlap: int = CHUNK_TOKEN_OVERLAP,
) -> list[str]:
    """Режет текст на чанки по ~chunk_size токенов с overlap токенов нахлёста
    между соседними. Порядок вывода строго == порядку в исходном тексте —
    вызывающий код (rag_ingest) проставляет chunk_index по позиции в списке.
    Пустой/пробельный текст -> пустой список чанков (не создаём мусорных строк)."""
    text = (text or "").strip()
    if not text:
        return []
    if overlap >= chunk_size:
        overlap = chunk_size // 4  # некорректная конфигурация — не уходим в бесконечный цикл

    enc = _encoding()
    if enc is None:
        return _chunk_by_words(text, chunk_size, overlap)

    tokens = enc.encode(text)
    if len(tokens) <= chunk_size:
        return [text]

    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    i = 0
    while i < len(tokens):
        piece_tokens = tokens[i:i + chunk_size]
        if not piece_tokens:
            break
        piece = enc.decode(piece_tokens).strip()
        if piece:
            chunks.append(piece)
        if i + chunk_size >= len(tokens):
            break
        i += step
    return chunks


def _chunk_by_words(text: str, chunk_size: int, overlap: int) -> list[str]:
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    i = 0
    while i < len(words):
        piece = " ".join(words[i:i + chunk_size]).strip()
        if piece:
            chunks.append(piece)
        if i + chunk_size >= len(words):
            break
        i += step
    return chunks
