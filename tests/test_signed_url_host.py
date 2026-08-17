"""Ссылка на файл проверяется по подписи, а не по хосту.

Клиенты законно подменяют хост на свой apiBase (мобильное приложение — fixUrl,
сайт — useFileUrl), иначе файлы не открываются с телефона и через туннель.
Раньше такой URL получал 400 «Недопустимый URL файла», и Word/презентации в
приложении не открывались вообще: конвертация в PDF отваливалась на первом же
шаге.
"""
import pytest
from fastapi import HTTPException

from routers.uploads import _signed_source_url, _verify_signed_upload_url
from services.file_urls import sign_upload_url


FILE = "r2/attachments/lecture.docx"


def _signed(base: str = "http://localhost:8000") -> str:
    return sign_upload_url(f"{base}/api/uploads/{FILE}")


def test_own_host_url_verifies():
    assert _verify_signed_upload_url(_signed()) == FILE


def test_client_rewritten_host_verifies():
    # Ровно то, что шлёт приложение: тот же путь и подпись, свой хост.
    rewritten = _signed().replace("localhost:8000", "192.168.0.10:8000")
    assert _verify_signed_upload_url(rewritten) == FILE


def test_fragment_with_original_filename_is_ignored():
    # К ссылке вложения клиенты добавляют «#Исходное имя.docx».
    assert _verify_signed_upload_url(f"{_signed()}#Лекция%201.docx") == FILE


def test_unsigned_url_rejected():
    with pytest.raises(HTTPException) as e:
        _verify_signed_upload_url(f"http://localhost:8000/api/uploads/{FILE}")
    assert e.value.status_code == 403


def test_forged_signature_rejected():
    url = _signed()
    forged = url.replace("sig=", "sig=0") if "sig=" in url else url
    with pytest.raises(HTTPException) as e:
        _verify_signed_upload_url(forged)
    assert e.value.status_code == 403


def test_foreign_path_rejected():
    with pytest.raises(HTTPException) as e:
        _verify_signed_upload_url("http://evil.example.com/secrets/passwd?exp=1&sig=deadbeef")
    assert e.value.status_code == 400


def test_source_url_is_built_from_own_base_not_from_client():
    """Скачиваем всегда со своего адреса: хост клиента в запрос не попадает."""
    source = _signed_source_url(FILE)
    assert source.startswith("http://localhost:8000/api/uploads/")
    assert "192.168.0.10" not in source
    assert _verify_signed_upload_url(source) == FILE
