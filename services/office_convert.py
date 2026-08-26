"""Конвертация легаси Office-форматов (.doc/.ppt/.xls/.rtf) в PDF через
headless LibreOffice (soffice).

Раньше эта логика жила только в routers/uploads.py (для превью), а пайплайн
чтения файлов ИИ (services/ai_grader._fetch_file_text) легаси-форматы вообще
не понимал: бинарный мусор .doc декодировался как utf-8 errors="ignore" и
уходил модели как «текст работы». Вынесено в сервис, чтобы оба потребителя
(превью и ИИ) использовали одну реализацию."""

import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# LibreOffice ищем не только в PATH: uvicorn, запущенный не из интерактивной
# оболочки (launchd, IDE, systemd), не видит /opt/homebrew/bin, и конвертация
# падала с «LibreOffice не установлен» на машине, где он стоит.
_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/snap/bin/libreoffice",
)


def soffice_path() -> str:
    """Путь к soffice: сначала PATH, затем обычные места установки."""
    import shutil

    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "soffice"  # пусть упадёт с понятной ошибкой ниже


def convert_office_to_pdf(content: bytes, ext: str) -> bytes:
    """Конвертирует .ppt/.pptx/.doc/.docx/.rtf/.xls в PDF через headless
    LibreOffice — блокирующий вызов, вызывать через run_in_threadpool /
    asyncio.to_thread. Каждый вызов — свой временный профиль/каталог
    (parallel-запросы иначе делят один профиль soffice и падают с блокировкой)."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, f"src.{ext}")
        with open(src, "wb") as f:
            f.write(content)
        try:
            result = subprocess.run(
                [
                    soffice_path(), "--headless", "--norestore", "--convert-to", "pdf",
                    "--outdir", tmpdir, src,
                ],
                capture_output=True, timeout=60,
                env={**os.environ, "HOME": tmpdir},
            )
        except FileNotFoundError as e:
            raise RuntimeError("LibreOffice (soffice) не установлен на сервере") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("Конвертация в PDF заняла слишком много времени") from e
        pdf_path = os.path.join(tmpdir, "src.pdf")
        if result.returncode != 0 or not os.path.isfile(pdf_path):
            stderr = result.stderr.decode(errors="ignore") if result.stderr else ""
            raise RuntimeError(f"Не удалось сконвертировать файл в PDF: {stderr[:300]}")
        with open(pdf_path, "rb") as f:
            return f.read()


def extract_pdf_text_from_bytes(pdf_bytes: bytes, max_pages: int = 40) -> str:
    """Текстовый слой PDF из байт (pdfplumber → pypdf фолбэк). Возвращает ""
    если текста нет или он «мусорный» (сломанный font-encoding)."""

    def _run() -> str:
        import io

        import pdfplumber

        pages = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = [p.extract_text(layout=True) or "" for p in pdf.pages[:max_pages]]
        except Exception:
            pages = []
        text = "\n\n".join(p for p in pages if p.strip())
        from services.ai_grader import _looks_garbled

        if text.strip() and not _looks_garbled(text):
            return text
        # pdfplumber не справился — пробуем pypdf (другой путь разбора шрифтов)
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages = [page.extract_text() or "" for page in reader.pages[:max_pages]]
            text = "\n\n".join(p for p in pages if p.strip())
            if text.strip() and not _looks_garbled(text):
                return text
        except Exception:
            pass
        return ""

    return _run()
