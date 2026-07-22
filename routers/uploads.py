import os
import logging
from collections import OrderedDict
from uuid import uuid4
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from deps import get_current_user
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from services.file_service import read_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Upload"])

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# zip/rar убраны: архивы не нужны учебному флоу и не читаются парсерами
ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "txt", "md", "csv", "rtf",
    "png", "jpg", "jpeg", "gif", "webp",
    "sm",
    "mp3", "wav", "m4a", "webm", "ogg", "mp4",
}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


MAGIC_BYTES: dict = {
    b"\x25\x50\x44\x46": "pdf",
    b"\x50\x4b\x03\x04": "zip/docx/xlsx/pptx",
    b"\xd0\xcf\x11\xe0": "doc/xls/ppt",
    b"\x89\x50\x4e\x47": "png",
    b"\xff\xd8\xff":     "jpg",
    b"\x47\x49\x46\x38": "gif",
    b"\x52\x49\x46\x46": "webp/wav",
    b"\x52\x61\x72\x21": "rar",
    b"\x1f\x8b":         "gz",
    b"\x49\x44\x33":     "mp3",
    b"\x1a\x45\xdf\xa3": "webm",
}


TEXT_EXTENSIONS = {"txt", "md", "csv", "rtf", "sm"}
AUDIO_VIDEO_EXTENSIONS = {"mp3", "wav", "m4a", "webm", "ogg", "mp4"}

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
        return content[:4] == b"\x52\x49\x46\x46"
    if ext == "rar":
        return content[:4] == b"\x52\x61\x72\x21"
    if ext in ("zip",):
        return content[:4] == b"\x50\x4b\x03\x04"
    if ext in ("docx", "xlsx", "pptx"):
        return content[:4] == b"\x50\x4b\x03\x04"
    if ext in ("doc", "xls", "ppt"):
        return content[:4] == b"\xd0\xcf\x11\xe0"
    if ext in AUDIO_VIDEO_EXTENSIONS:

        return True

    return True


# Файлы в uploads/ неизменяемые (UUID-имена), поэтому распарсенный текст кэшируем:
# без этого каждый вход в класс заново качал и парсил все файлы.
_file_text_cache: "OrderedDict[str, str]" = OrderedDict()
_FILE_TEXT_CACHE_MAX = 256


@router.get("/utils/file-text")
async def get_file_text(
    url: str,
    current_user=Depends(get_current_user),
):
    # Эндпоинт нужен и студентам (мобильный клиент собирает AI-контекст класса).
    # Одной SSRF-проверки мало: без подписи любой авторизованный вытащил бы текст
    # чужой сдачи по угаданному URL (SEC-6).
    from services.url_safety import is_safe_fetch_url
    if not is_safe_fetch_url(url):
        raise HTTPException(status_code=400, detail="Недопустимый URL файла")

    from urllib.parse import urlparse, parse_qs
    from services.file_urls import verify_signature
    parsed = urlparse(url)
    prefix = "/uploads/"
    if not parsed.path.startswith(prefix):
        raise HTTPException(status_code=400, detail="Недопустимый URL файла")
    file_path = parsed.path[len(prefix):]
    qs = parse_qs(parsed.query)
    exp = (qs.get("exp") or [None])[0]
    sig = (qs.get("sig") or [None])[0]
    if not verify_signature(file_path, exp, sig):
        raise HTTPException(status_code=403, detail="Недействительная или просроченная ссылка")

    # Кэш по пути файла (подпись меняется от запроса к запросу).
    cached = _file_text_cache.get(file_path)
    if cached is not None:
        _file_text_cache.move_to_end(file_path)
        return {"text": cached}
    from services.ai_grader import _fetch_file_text
    text = await _fetch_file_text(url)
    if text:
        _file_text_cache[file_path] = text
        if len(_file_text_cache) > _FILE_TEXT_CACHE_MAX:
            _file_text_cache.popitem(last=False)
    return {"text": text}


@router.post("/")
async def upload_file(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

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

    unique_filename = f"{uuid4().hex}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)

    # Запись файла и парсинг (pdfplumber/python-docx/openpyxl/OCR) — блокирующие
    # операции; в async-обработчике их нельзя выполнять напрямую, иначе они
    # заморозят весь event loop и все параллельные запросы. Выносим в пул потоков.
    def _write() -> None:
        with open(file_path, "wb") as buffer:
            buffer.write(content)

    try:
        await run_in_threadpool(_write)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    # Повреждённый PDF/DOCX роняет парсер исключением, а раньше это улетало как 500.
    # Тип уже подтверждён magic-байтами, текст нужен только ИИ — деградируем молча.
    try:
        parsed = await run_in_threadpool(read_file, file_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось распарсить %s (%s): %s", file.filename, unique_filename, e)
        parsed = {"type": "unparsed", "error": "Не удалось извлечь текст из файла"}

    file_url = f"{APP_BASE_URL.rstrip('/')}/uploads/{unique_filename}"
    return JSONResponse(content={"file_url": file_url, "filename": file.filename, "parsed": parsed})
