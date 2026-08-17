import os
import logging
import tempfile
from collections import OrderedDict
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from deps import get_current_user
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.storage import CATEGORIES, PREVIEW_CATEGORY, StorageError, get_storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Upload"])

# zip/rar убраны: архивы не нужны учебному флоу и не читаются парсерами
# heic/heif: клиент (upload_limits.dart) давно разрешает их в пикере файлов
# (дефолтный формат камеры iPhone), а бэкенд эти расширения тупо отклонял —
# студент с iPhone не мог сдать фото домашки, если Settings > Camera >
# Formats стоит "High Efficiency" (значение по умолчанию), пока сам не
# сконвертирует файл руками. См. также services/ai_grader.py — HEIC для
# GPT-4o vision конвертируется в JPEG (OpenAI не принимает image/heic).
ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "txt", "md", "csv", "rtf",
    "png", "jpg", "jpeg", "gif", "webp", "heic", "heif",
    "sm",
    "mp3", "wav", "m4a", "webm", "ogg", "mp4",
}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

_CONTENT_TYPE_BY_EXT = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "rtf": "application/rtf",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "heic": "image/heic", "heif": "image/heif",
    "sm": "text/plain",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "webm": "video/webm",
    "ogg": "audio/ogg", "mp4": "video/mp4",
}


TEXT_EXTENSIONS = {"txt", "md", "csv", "rtf", "sm"}

# HEIC/HEIF — контейнер ISO base media file format (тот же "скелет", что у
# MP4): байты 4:8 всегда "ftyp", 8:12 — код бренда. Значения ниже покрывают
# и фото (heic/heix/mif1...), и live-фото/burst-варианты (heic вариаций
# достаточно много у разных версий iOS).
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
                b"hevm", b"hevs", b"mif1", b"msf1"}

def _validate_file_content(content: bytes, ext: str) -> bool:

    if ext in TEXT_EXTENSIONS:

        try:
            content[:1024].decode("utf-8", errors="strict")
            return True
        except UnicodeDecodeError:
            try:
                content[:1024].decode("cp1251", errors="strict")
                return True
            except Exception:
                return False
    if ext == "pdf":
        return content[:4] == b"\x25\x50\x44\x46"
    if ext in ("png",):
        return content[:4] == b"\x89\x50\x4e\x47"
    if ext in ("jpg", "jpeg"):
        return content[:3] == b"\xff\xd8\xff"
    if ext == "gif":
        return content[:4] == b"\x47\x49\x46\x38"
    if ext == "webp":
        # RIFF — общий контейнер (тот же заголовок и у wav); без проверки
        # тега формата в байтах 8:12 любой RIFF-файл (например, .wav) молча
        # принимался бы как webp.
        return content[:4] == b"\x52\x49\x46\x46" and content[8:12] == b"WEBP"
    if ext in ("heic", "heif"):
        return content[4:8] == b"ftyp" and content[8:12] in _HEIF_BRANDS
    if ext == "rar":
        return content[:4] == b"\x52\x61\x72\x21"
    if ext in ("zip",):
        return content[:4] == b"\x50\x4b\x03\x04"
    if ext in ("docx", "xlsx", "pptx"):
        return content[:4] == b"\x50\x4b\x03\x04"
    if ext in ("doc", "xls", "ppt"):
        return content[:4] == b"\xd0\xcf\x11\xe0"
    if ext == "wav":
        return content[:4] == b"\x52\x49\x46\x46" and content[8:12] == b"WAVE"
    if ext == "ogg":
        return content[:4] == b"OggS"
    if ext == "webm":
        return content[:4] == b"\x1a\x45\xdf\xa3"
    if ext in ("mp4", "m4a"):
        # ISO base media file format (тот же "скелет", что у heic выше) — брендов
        # у mp4/m4a контейнеров слишком много (isom/mp42/M4A ...), достаточно
        # убедиться, что это действительно ISO-BMF, а не что-то ещё под чужим
        # расширением.
        return content[4:8] == b"ftyp"
    if ext == "mp3":
        if content[:3] == b"ID3":
            return True
        # Без ID3-тега mp3 начинается прямо с frame sync: 11 единичных бит
        # (0xFF + верхние 3 бита следующего байта).
        return len(content) >= 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0

    return True


# Файлы в uploads/ неизменяемые (UUID-имена), поэтому распарсенный текст кэшируем:
# без этого каждый вход в класс заново качал и парсил все файлы.
_file_text_cache: "OrderedDict[str, str]" = OrderedDict()
_FILE_TEXT_CACHE_MAX = 256


def _verify_signed_upload_url(url: str) -> str:
    """Проверяет подпись файлового URL (SEC-6: без этого любой авторизованный
    вытащил бы чужой файл по угаданному пути) и возвращает file_path (без базы
    и query). Бросает HTTPException при недопустимом/просроченном URL.

    Хост в переданном URL НЕ проверяется намеренно. Подпись считается только от
    пути файла, а клиенты законно подменяют хост на свой apiBase: мобильное
    приложение (fixUrl в api_service.dart) и сайт (composables/useFileUrl.ts)
    делают это, чтобы файлы открывались с телефона и из туннеля, где домен
    APP_BASE_URL не резолвится. Раньше такой URL получал 400 «Недопустимый URL
    файла» — из-за этого Word и презентации не открывались в приложении вовсе.

    От SSRF защищает не хост запроса, а сама подпись: путь без валидной
    подписи сюда не проходит, а качать файл вызывающий код обязан по адресу,
    собранному из APP_BASE_URL (см. signed_source_url ниже), а не по тому, что
    пришло от клиента.
    """
    from urllib.parse import urlparse, parse_qs, unquote
    from services.file_urls import verify_signature
    parsed = urlparse(url)
    prefix = "/api/uploads/"
    legacy_prefix = "/uploads/"
    # urlparse() не раскодирует %XX в пути (в отличие от Starlette-роута
    # /api/uploads/{filename:path}, который отдаёт filename уже декодированным) —
    # без unquote() здесь подпись, посчитанная от декодированного пути, не
    # проходила бы проверку, если исходный url пришёл с percent-encoded путём.
    if parsed.path.startswith(prefix):
        file_path = unquote(parsed.path[len(prefix):])
    elif parsed.path.startswith(legacy_prefix):
        file_path = unquote(parsed.path[len(legacy_prefix):])
    else:
        raise HTTPException(status_code=400, detail="Недопустимый URL файла")
    qs = parse_qs(parsed.query)
    exp = (qs.get("exp") or [None])[0]
    sig = (qs.get("sig") or [None])[0]
    if not verify_signature(file_path, exp, sig):
        raise HTTPException(status_code=403, detail="Недействительная или просроченная ссылка")
    return file_path


def _signed_source_url(file_path: str) -> str:
    """Свежая подписанная ссылка на свой же файл — по ней сервер и качает.
    Никогда не ходим по адресу, который прислал клиент (он мог подменить хост)."""
    from services.file_urls import sign_upload_url
    from urllib.parse import quote
    base = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    return sign_upload_url(f"{base}/api/uploads/{quote(file_path)}")


@router.get("/utils/file-text")
async def get_file_text(
    url: str,
    current_user=Depends(get_current_user),
):
    # Эндпоинт нужен и студентам (мобильный клиент собирает AI-контекст класса).
    file_path = _verify_signed_upload_url(url)

    # Кэш по пути файла (подпись меняется от запроса к запросу).
    cached = _file_text_cache.get(file_path)
    if cached is not None:
        _file_text_cache.move_to_end(file_path)
        return {"text": cached}
    from services.ai_grader import _fetch_file_text
    text = await _fetch_file_text(_signed_source_url(file_path))
    if text:
        _file_text_cache[file_path] = text
        if len(_file_text_cache) > _FILE_TEXT_CACHE_MAX:
            _file_text_cache.popitem(last=False)
    return {"text": text}


# .doc/.rtf/.ppt/.pptx — форматы, для которых в браузере нет разумного
# рендерера. docx здесь тоже есть, хотя сайт рисует его сам (docx-preview):
# в приложении своего рендерера docx нет, а PDF-версия открывается в общем
# просмотрщике — с выделениями и заметками, как у остальных материалов.
_OFFICE_PREVIEW_EXTS = {"ppt", "pptx", "doc", "docx", "rtf"}

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


def _soffice_path() -> str:
    """Путь к soffice: сначала PATH, затем обычные места установки."""
    import shutil
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "soffice"  # пусть упадёт с понятной ошибкой ниже


def _convert_office_to_pdf(content: bytes, ext: str) -> bytes:
    """Конвертирует .ppt/.pptx/.doc/.rtf в PDF через headless LibreOffice
    (soffice) — блокирующий вызов, вызывать через run_in_threadpool. Каждый
    вызов — свой временный профиль/каталог (parallel-запросы иначе делят один
    профиль soffice и падают с блокировкой)."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmpdir:
        src = os.path.join(tmpdir, f"src.{ext}")
        with open(src, "wb") as f:
            f.write(content)
        try:
            result = subprocess.run(
                [
                    _soffice_path(), "--headless", "--norestore", "--convert-to", "pdf",
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


@router.get("/utils/preview-pdf")
async def get_preview_pdf(
    url: str,
    current_user=Depends(get_current_user),
):
    """PDF-версия .ppt/.pptx/.doc/.rtf для предпросмотра на сайте (родной
    PDF-вьюер браузера через iframe — без него такие файлы показывались
    только с кнопкой «скачать», см. FilePreviewModal.vue). Результат
    кэшируется в R2 под детерминированным ключом от пути исходника:
    конвертация — не бесплатная операция (LibreOffice), а сам исходник
    неизменяем."""
    file_path = _verify_signed_upload_url(url)
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if ext not in _OFFICE_PREVIEW_EXTS:
        raise HTTPException(status_code=422, detail="Предпросмотр в PDF доступен только для .ppt/.pptx/.doc/.rtf")

    import hashlib
    cache_key = f"{PREVIEW_CATEGORY}/{hashlib.sha256(file_path.encode()).hexdigest()}.pdf"
    storage = get_storage_service()

    if not await run_in_threadpool(storage.exists, cache_key):
        signed_source = _signed_source_url(file_path)
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(signed_source)
        if not resp.is_success:
            raise HTTPException(status_code=502, detail="Не удалось получить исходный файл")
        try:
            pdf_bytes = await run_in_threadpool(_convert_office_to_pdf, resp.content, ext)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        try:
            await run_in_threadpool(storage.upload, pdf_bytes, cache_key, "application/pdf")
        except StorageError as e:
            raise HTTPException(status_code=502, detail=f"Не удалось сохранить предпросмотр: {e}")

    pdf_url = await run_in_threadpool(storage.get_url, cache_key)
    return {"pdf_url": pdf_url}


DEFAULT_CATEGORY = "attachments"


@router.post("/")
async def upload_file(
    file: UploadFile = File(...),
    category: str = Form(DEFAULT_CATEGORY),
    current_user=Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if category not in CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"category должна быть одной из {sorted(CATEGORIES)}",
        )

    ext = os.path.splitext(file.filename)[1].lstrip(".").lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Тип файла '.{ext}' не разрешён. Допустимые: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Читаем потоково и рвём на превышении лимита: буферизация целиком до проверки
    # позволяла исчерпать RAM большим аплоадом (BE-10).
    content = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="Файл слишком большой (максимум 50 МБ)")
    content = bytes(content)


    if not _validate_file_content(content, ext):
        raise HTTPException(
            status_code=415,
            detail=f"Содержимое файла не соответствует расширению .{ext}. Загрузите настоящий {ext.upper()} файл.",
        )

    storage = get_storage_service()
    content_type = _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")

    # Текст файла (pdfplumber/OCR/...) здесь больше НЕ извлекается: ни клиент,
    # ни бэкенд не читают поле parsed из ответа — извлечение текста нужно
    # только для ИИ (грейдинг/контекст чата) и делается лениво, по требованию,
    # через GET /upload/utils/file-text (см. services/ai_grader._fetch_file_text),
    # с собственным кэшем. Раньше read_file() (в т.ч. тяжёлый EasyOCR/pdfplumber)
    # гонялся синхронно на каждую загрузку и держал клиента по 10-30+ секунд
    # на файл — не давая при этом никакого результата, который бы кто-то читал.
    try:
        # upload_unique — не build_key()+upload(): та пара делала exists()-
        # проверку и запись отдельными вызовами, и два параллельных запроса
        # с одинаковым именем файла в одной категории могли получить один и
        # тот же "свободный" ключ, после чего второй PUT молча перезаписывал
        # файл первого без единой ошибки. upload_unique пишет атомарно.
        file_url = await run_in_threadpool(
            storage.upload_unique, content, category, file.filename, content_type,
        )
    except StorageError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось загрузить файл в хранилище: {e}")

    return JSONResponse(content={"file_url": file_url, "filename": file.filename})
