"""Сканы/фото-конспекты, вставленные в PDF без текстового слоя, раньше
доходили до модели ТОЛЬКО через слабый OCR-текст (tesseract) — рукописный
текст и особенно формулы регулярно превращались в мусор. Тот же контент,
загруженный как отдельный .jpg, уже читается через GPT-4o vision. Студент не
должен получать разное качество проверки в зависимости от того, сохранил ли
телефон скан как .jpg или как .pdf."""
import asyncio
import io

import pytest
from PIL import Image

from services import ai_grader


def _image_only_pdf_bytes(color=(120, 40, 200)) -> bytes:
    """PDF без текстового слоя вообще — как "Печать в PDF" со скана/фото."""
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), color=color).save(buf, format="PDF")
    return buf.getvalue()


def _text_pdf_bytes() -> bytes:
    """Минимальный, но валидный для pdfplumber/pdfminer PDF с текстовым
    слоем — обычный "печатный" документ, картинки страниц ему не нужны."""
    return b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 300 300] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length 54 >>
stream
BT /F1 24 Tf 10 100 Td (Hello real text layer) Tj ET
endstream
endobj
trailer
<< /Size 6 /Root 1 0 R >>
%%EOF
"""


class _FakeGetClient:
    def __init__(self, content: bytes):
        self._content = content

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        class _R:
            is_success = True
            status_code = 200
            content = self._content
            headers = {"content-type": "application/pdf"}
        return _R()


def test_scanned_pdf_without_text_layer_renders_page_images(monkeypatch):
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeGetClient(_image_only_pdf_bytes()))
    uris = asyncio.run(ai_grader._fetch_pdf_page_images("http://localhost:8000/api/uploads/scan.pdf"))
    assert len(uris) == 1
    assert uris[0].startswith("data:image/jpeg;base64,")


def test_normal_text_pdf_does_not_render_page_images(monkeypatch):
    """Обычный печатный PDF с текстовым слоем не должен тратить vision-бюджет
    на картинки страниц — текстовой экстракции уже достаточно."""
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeGetClient(_text_pdf_bytes()))
    uris = asyncio.run(ai_grader._fetch_pdf_page_images("http://localhost:8000/api/uploads/typed.pdf"))
    assert uris == []


def test_fetch_pdf_page_images_skips_non_pdf_urls(monkeypatch):
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeGetClient(_image_only_pdf_bytes()))
    uris = asyncio.run(ai_grader._fetch_pdf_page_images("http://localhost:8000/api/uploads/scan.docx"))
    assert uris == []


def test_fetch_pdf_page_images_corrupted_bytes_returns_empty_not_raise(monkeypatch):
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeGetClient(b"not a pdf at all"))
    uris = asyncio.run(ai_grader._fetch_pdf_page_images("http://localhost:8000/api/uploads/broken.pdf"))
    assert uris == []


def test_fetch_pdf_page_images_respects_max_pages_cap(monkeypatch):
    """Многостраничный скан не должен рендерить больше MAX_SCANNED_PDF_PAGES —
    иначе автопроверка/ручная кнопка виснет на десятках vision-страниц."""
    buf = io.BytesIO()
    pages = [Image.new("RGB", (100, 100), color=(i, 0, 0)) for i in range(10)]
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    monkeypatch.setattr(ai_grader.httpx, "AsyncClient", _FakeGetClient(buf.getvalue()))
    uris = asyncio.run(ai_grader._fetch_pdf_page_images("http://localhost:8000/api/uploads/scan.pdf"))
    assert len(uris) == ai_grader.MAX_SCANNED_PDF_PAGES
