"""Регрессия: R2-ключи теперь хранят оригинальное имя файла (юникод, пробелы)
вместо UUID (см. services/storage/base.py). services/file_urls.py должен
правильно подписывать такие пути и не расползаться на соседний текст."""
import json
import re

from services.file_urls import sign_uploads_in_text, verify_signature


def test_signs_absolute_r2_url_with_cyrillic_and_spaces():
    # "Домашнее задание 1.docx" — ровно то, что реально попадёт в ключ после
    # StorageService.build_key/_sanitize_filename (не-словарные символы вроде
    # "№" туда не попадают, они заменяются на "_" ещё при генерации ключа).
    body = json.dumps({
        "file_url": "http://localhost:8000/uploads/r2/attachments/Домашнее задание 1.docx",
    }, ensure_ascii=False)

    out = json.loads(sign_uploads_in_text(body))
    m = re.match(
        r"http://localhost:8000/api/uploads/(r2/attachments/Домашнее задание 1\.docx)\?exp=(\d+)&sig=(\w+)$",
        out["file_url"],
    )
    assert m, out["file_url"]
    assert verify_signature(m.group(1), m.group(2), m.group(3))


def test_signs_relative_r2_url_with_spaces_inside_brackets():
    body = json.dumps({
        "markdown": "См. [Отчёт](/uploads/r2/materials/Мой отчёт.pdf) ниже",
    }, ensure_ascii=False)

    out = json.loads(sign_uploads_in_text(body))
    m = re.search(r"/uploads/(r2/materials/Мой отчёт\.pdf)\?exp=(\d+)&sig=(\w+)\)", out["markdown"])
    assert m, out["markdown"]
    assert verify_signature(m.group(1), m.group(2), m.group(3))


def test_does_not_consume_trailing_prose_after_quoted_value():
    # Значение — целиком URL (ограничено кавычками JSON), после закрывающей
    # кавычки идёт следующий JSON-ключ. Путь не должен "перетечь" в него.
    body = (
        '{"file_url": "http://localhost:8000/uploads/r2/materials/Мой файл.pdf", '
        '"note": "не трогать"}'
    )
    out = json.loads(sign_uploads_in_text(body))
    assert out["note"] == "не трогать"
    assert out["file_url"].startswith(
        "http://localhost:8000/api/uploads/r2/materials/Мой файл.pdf?exp="
    )


def test_signs_percent_encoded_path_with_fragment_name():
    # Регрессия: клиенты (Nuxt encodeURI(), Flutter Uri.encodeComponent())
    # percent-кодируют путь ДО того, как вставить ссылку в description —
    # значит путь в самом JSON уже содержит "%20" и т.п. Раньше regex
    # обрывался на первом "%" (path матчился только как "card"), и хвост
    # ("%202.jpg?exp=...&sig=...#card%202.jpg") оставался в тексте как
    # видимый пользователю мусор вместо переподписанной ссылки.
    #
    # Путь должен подписываться в ДЕКОДИРОВАННОМ виде: именно его отдаёт
    # Starlette в {filename:path} на GET /api/uploads/... (ASGI раскодирует
    # %XX в пути запроса до роутинга), поэтому именно от него и должна
    # совпасть подпись — иначе только что подписанная ссылка сама не
    # проходила бы verify_signature (см. баг: 403 сразу после подписи).
    body = json.dumps({
        "description": (
            "http://localhost:8000/api/uploads/r2/attachments/card%202.jpg"
            "?exp=1786559225&sig=aeba31b1e066483db2a93934a1c6078d4e0c4d4df1cca2322352b1574a228d38"
            "#card%202.jpg"
        ),
    }, ensure_ascii=False)

    out = json.loads(sign_uploads_in_text(body))
    m = re.match(
        r"http://localhost:8000/api/uploads/(r2/attachments/card 2\.jpg)\?exp=(\d+)&sig=(\w+)#card%202\.jpg$",
        out["description"],
    )
    assert m, out["description"]
    assert verify_signature(m.group(1), m.group(2), m.group(3))


def test_does_not_cross_json_string_boundary_in_free_text():
    # Гипотетический случай: относительная ссылка встречается посреди
    # свободного текста без скобок/кавычек вокруг. Подпись не должна вылезти
    # за пределы этой JSON-строки и захватить остальной текст без разбора.
    body = json.dumps({
        "content": "Файл /uploads/r2/materials/Заметка.pdf лежит здесь, остальное не трогаем.",
    }, ensure_ascii=False)

    out = json.loads(sign_uploads_in_text(body))
    # Подписанный URL должен оставаться внутри исходной строки — JSON остаётся валиден
    # и после него по-прежнему видна остальная часть предложения.
    assert out["content"].startswith("Файл ")
    assert "?exp=" in out["content"]
    assert "sig=" in out["content"]