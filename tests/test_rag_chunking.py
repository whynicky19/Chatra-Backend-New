"""services/chunking.py — разбиение текста лекции на чанки для RAG."""
from services import chunking


def test_chunk_text_empty_returns_empty_list():
    assert chunking.chunk_text("") == []
    assert chunking.chunk_text("   \n\t  ") == []


def test_chunk_text_short_text_single_chunk():
    text = "Короткий текст лекции в один абзац."
    chunks = chunking.chunk_text(text)
    assert chunks == [text]


def test_chunk_text_long_text_splits_into_multiple_chunks():
    text = "Слово раз. " * 2000
    chunks = chunking.chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    for c in chunks:
        assert chunking.count_tokens(c) <= 120  # с запасом на округление токенизации


def test_chunk_text_preserves_order():
    # Каждое "слово" уникально — легко проверить, что чанки идут в порядке
    # исходного текста, а не перемешаны.
    words = [f"w{i}" for i in range(500)]
    text = " ".join(words)
    chunks = chunking.chunk_text(text, chunk_size=50, overlap=5)
    reassembled = " ".join(chunks)
    # w0 должно встретиться раньше w499 в объединённом тексте чанков.
    assert reassembled.index("w0") < reassembled.index("w499")


def test_chunk_text_overlap_creates_continuity_between_chunks():
    words = [f"w{i}" for i in range(300)]
    text = " ".join(words)
    chunks = chunking.chunk_text(text, chunk_size=50, overlap=10)
    assert len(chunks) >= 2
    # Хвост первого чанка должен пересекаться с началом второго (overlap).
    tail_of_first = chunks[0].split()[-5:]
    head_of_second = chunks[1].split()[:15]
    assert any(w in head_of_second for w in tail_of_first)


def test_chunk_text_does_not_infinite_loop_on_bad_overlap_config():
    text = "слово " * 500
    # overlap >= chunk_size — раньше могло привести к бесконечному циклу
    # (step = chunk_size - overlap <= 0).
    chunks = chunking.chunk_text(text, chunk_size=50, overlap=999)
    assert len(chunks) > 0  # завершилось, не зависло


def test_count_tokens_empty_string():
    assert chunking.count_tokens("") == 0


def test_count_tokens_roughly_matches_word_count_order_of_magnitude():
    text = "один два три четыре пять"
    n = chunking.count_tokens(text)
    assert 1 <= n <= 20  # разумный диапазон, не точное совпадение (BPE != слова)


def test_chunk_text_word_fallback_used_when_no_encoding(monkeypatch):
    """Если tiktoken недоступен офлайн (нет сети на первой загрузке словаря),
    пайплайн не должен падать — переключается на фолбэк по словам."""
    monkeypatch.setattr(chunking, "_encoding", lambda: None)
    text = "слово " * 300
    chunks = chunking.chunk_text(text, chunk_size=50, overlap=10)
    assert len(chunks) > 1
    assert " ".join(chunks).split()[0] == "слово"
