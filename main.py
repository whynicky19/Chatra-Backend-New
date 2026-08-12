from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import mimetypes
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from services.file_urls import sign_uploads_in_text, verify_signature
from services.storage import StorageError, get_storage_service, is_public_key
from db import engine
from models import Base
from routers import auth, admin, users, posts, uploads, ai, notifications, push, moderation
from routers.assignments import router as assignments_router
from routers.classes import router as classes_router, rating_router
from routers.cohorts import router as cohorts_router
from sqlalchemy import text
from services.deadline_checker import deadline_checker_loop
from services.deadline_reminder import deadline_reminder_loop

logging.basicConfig(level=logging.INFO)

Base.metadata.create_all(bind=engine)

def _check_db():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logging.info("Database connection OK")
    except Exception as e:
        logging.error(f"Database connection failed: {e}")

_check_db()

# Схемы university/school не создаём: изоляция идёт по колонке org_type (BE-3).

_cors_raw = os.getenv("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_raw.split(",")] if _cors_raw != "*" else ["*"]
# "*" + credentials = отключённый CORS, поэтому с "*" работаем без credentials.
# В проде задайте явный список в CORS_ORIGINS.
_cors_credentials = _cors_origins != ["*"]
if not _cors_credentials:
    logging.warning(
        "CORS_ORIGINS='*' — allow_credentials отключён. "
        "В проде задайте явный список origin'ов в CORS_ORIGINS."
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(deadline_checker_loop())
    reminder_task = asyncio.create_task(deadline_reminder_loop())
    try:
        yield
    finally:
        for t in (task, reminder_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

app = FastAPI(title="Chatra API", version="3.0", lifespan=lifespan)


class UploadUrlSignerMiddleware(BaseHTTPMiddleware):
    """Подписывает ссылки на /uploads в JSON-ответах (SEC-1): доступа по «голому»
    URL нет. Не-JSON проходит насквозь, без буферизации."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if not ctype.startswith("application/json"):
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            signed = sign_uploads_in_text(body.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            signed = body
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=signed,
            status_code=response.status_code,
            headers=headers,
            media_type=ctype,
        )


class _UploadsSkippingGZipMiddleware(GZipMiddleware):
    """GZip не должен трогать /api/uploads: Content-Encoding: gzip несовместим
    с Range/206 (Content-Range описывает офсеты в НЕсжатом теле — многие
    мобильные HTTP-стеки/плееры не умеют это сопоставлять и не могут
    перематывать видео/аудио, вплоть до отказа воспроизводить файл вовсе), а
    бинарные файлы (jpg/mp4/pdf/...) и так уже сжаты — gzip на них только жжёт
    CPU без выигрыша в размере."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/uploads/"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# Порядок важен: signer первый = самый внутренний, видит несжатый JSON.
# GZip добавляется после и сжимает уже подписанное тело.
app.add_middleware(UploadUrlSignerMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Сжатие JSON-ответов: списки постов/классов с текстами лекций ужимаются в разы.
app.add_middleware(_UploadsSkippingGZipMiddleware, minimum_size=1024)

# Все публичные API-эндпоинты живут под /api: на проде под теми же путями,
# что и API (например /classes/27), отдаются и страницы веб-фронтенда, из-за
# чего nginx их путает. /api отделяет бэкенд от фронтенда однозначно.
app.include_router(auth.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
# Модерация — до users: GET /users/blocked обязан объявляться раньше любого
# будущего /users/{id}, иначе "blocked" уедет в path-параметр.
app.include_router(moderation.router, prefix="/api")
app.include_router(moderation.admin_router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(posts.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(push.router, prefix="/api")
app.include_router(assignments_router, prefix="/api")
app.include_router(classes_router, prefix="/api")
app.include_router(cohorts_router, prefix="/api")
app.include_router(rating_router, prefix="/api")

_upload_dir = os.getenv("UPLOAD_DIR", "uploads")
os.makedirs(_upload_dir, exist_ok=True)

# Типы, которые безопасно отдавать inline (просмотр в браузере/приложении);
# всё остальное — только скачиванием, чтобы браузер не исполнял содержимое.
_INLINE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "mp3", "wav", "m4a", "webm", "ogg", "mp4",
    "pdf",  # нужен для iframe-предпросмотра на фронте
}

_uploads_root = os.path.realpath(_upload_dir)


def _content_disposition(disposition: str, download_name: str) -> str:
    ascii_name = download_name.encode("ascii", "replace").decode().replace('"', "'")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(download_name)}'


_PUBLIC_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _serve_r2_upload(filename: str, name: str | None, request: Request):
    """Отдаёт файл из R2 (новые загрузки), подпись уже проверена в serve_upload.
    filename имеет вид 'r2/<key>'; ключ в бакете — без префикса 'r2/'.

    Один GET к R2 вместо HEAD+GET (было два round-trip'а на каждый запрос) —
    404 ловится по ответу самого GET. Пробрасывает Range (206 Partial
    Content — важно для перемотки аудио/видео и прогрессивной загрузки
    картинок) и ETag/Cache-Control из R2, с фолбэком Cache-Control для
    публичных категорий (обложки классов), даже пока R2_PUBLIC_BASE_URL не
    настроен и раздача всё ещё идёт через этот прокси."""
    key = filename[len("r2/"):]
    storage = get_storage_service()

    inm = request.headers.get("if-none-match")

    try:
        obj = storage.get_object(key, range_header=request.headers.get("range"))
    except StorageError:
        raise HTTPException(status_code=502, detail="File storage temporarily unavailable")
    if obj is None:
        raise HTTPException(status_code=404, detail="File not found")

    if inm and obj.etag and inm.strip('"') == obj.etag:
        return Response(status_code=304, headers={"ETag": f'"{obj.etag}"'})

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    disposition = "inline" if ext in _INLINE_EXTENSIONS else "attachment"
    download_name = os.path.basename(key)
    if name:
        cleaned = name.replace("\r", "").replace("\n", "").strip()
        if cleaned:
            download_name = cleaned[:255]
    # Content-Type — по РЕАЛЬНОМУ ключу файла, не по download_name: `name` не
    # входит в подпись и полностью подконтролен клиенту (см. комментарий у
    # serve_upload про polyglot-файл + "&name=x.html" → inline HTML → XSS в
    # origin бэкенда, если бы Content-Type зависел от него).
    content_type = mimetypes.guess_type(key)[0] or obj.content_type

    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(disposition, download_name),
        "Accept-Ranges": "bytes",
    }
    if obj.etag:
        headers["ETag"] = f'"{obj.etag}"'
    cache_control = obj.cache_control or (_PUBLIC_CACHE_CONTROL if is_public_key(key) else None)
    if cache_control:
        headers["Cache-Control"] = cache_control
    if obj.partial and obj.content_range:
        headers["Content-Range"] = obj.content_range
        status_code = 206
    else:
        status_code = 200

    return Response(content=obj.body, media_type=content_type, headers=headers, status_code=status_code)


@app.get("/api/uploads/{filename:path}")
def serve_upload(
    filename: str,
    request: Request,
    exp: int = Query(...),
    sig: str = Query(...),
    name: str | None = Query(None),
):
    """Отдаёт файл только по действующей подписи (SEC-1). Подпись выдаётся вместе
    с ресурсом, поэтому получить её может лишь тот, кто вправе его прочитать.

    Необязательный `name` задаёт ОРИГИНАЛЬНОЕ имя файла в Content-Disposition,
    чтобы при скачивании юзер получал «Отчёт.pdf», а не «<uuid>.pdf». Имя не
    входит в подпись (оно косметическое и не даёт доступа), поэтому клиент
    добавляет его сам из #fragment сохранённой ссылки. Starlette сам кодирует
    не-ASCII имена (filename*), CR/LF вырезаем от инъекции заголовка.
    """
    if not verify_signature(filename, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired file link")

    if filename.startswith("r2/"):
        return _serve_r2_upload(filename, name, request)

    full = os.path.realpath(os.path.join(_uploads_root, filename))
    # Защита от path traversal (../): результат обязан лежать внутри uploads.
    if full != _uploads_root and not full.startswith(_uploads_root + os.sep):
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    disposition = "inline" if ext in _INLINE_EXTENSIONS else "attachment"
    download_name = os.path.basename(full)
    if name:
        cleaned = name.replace("\r", "").replace("\n", "").strip()
        if cleaned:
            download_name = cleaned[:255]
    # media_type считаем от РЕАЛЬНОГО файла на диске, а не от download_name:
    # `name` не входит в подпись (см. докстринг выше) и полностью подконтролен
    # клиенту — если бы Content-Type определялся по нему (как делает Starlette
    # по умолчанию, когда filename= передан без явного media_type), достаточно
    # было бы приписать "&name=x.html" к любой валидной подписанной ссылке на
    # файл с inline-расширением, чтобы браузер отрендерил его как HTML при
    # X-Content-Type-Options: nosniff (который не защищает от ЯВНО неверного
    # Content-Type, только от угадывания) — polyglot-файл (JPEG/PNG с валидными
    # первыми байтами и HTML-payload дальше, magic-byte проверка на загрузке
    # смотрит только на сигнатуру) исполнялся бы как скрипт в origin бэкенда.
    media_type = mimetypes.guess_type(full)[0]
    # filename обязателен: без него Starlette вообще не шлёт Content-Disposition,
    # и content_disposition_type ни на что не влияет.
    return FileResponse(
        full,
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type=disposition,
        filename=download_name,
        media_type=media_type,
    )


@app.get("/health")
def health():
    return {"status": "ok"}
