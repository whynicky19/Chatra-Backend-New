from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from services.file_urls import sign_uploads_in_text, verify_signature
from db import Base, engine
from models import Base
from routers import auth, admin, users, posts, chats, messages, reactions, uploads, ai, avatars, notifications, push, reports
from routers.assignments import router as assignments_router
from routers.classes import router as classes_router, rating_router
from routers.cohorts import router as cohorts_router
from routers.rag import router as rag_router
from websocket import router as ws_router
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

# BE-3: раньше здесь создавались отдельные схемы university/school и таблицы в
# них — второй, несовместимый с колонкой org_type механизм изоляции. На проде
# он не работал (схемы пусты, данные в public) и лишь плодил движки/пулы.
# Изоляция организаций держится на колонке org_type; отдельные схемы убраны.

_cors_raw = os.getenv("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_raw.split(",")] if _cors_raw != "*" else ["*"]
# Wildcard-origin вместе с credentials — это фактически отключенный CORS:
# браузеру отражается любой Origin. С "*" работаем без credentials;
# в проде задайте явный список в CORS_ORIGINS.
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
    """SEC-1: подписывает все ссылки на /uploads в JSON-ответах. Клиент
    получает временную подписанную ссылку, по которой прокси отдаёт файл;
    публичного доступа по «голому» URL больше нет. Не-JSON (сами файлы,
    стримы) проходят насквозь без буферизации."""

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


# ВАЖЕН порядок: signer добавляется ПЕРВЫМ, значит он самый внутренний и
# видит несжатый JSON. GZip добавляется позже и сжимает уже подписанное тело.
app.add_middleware(UploadUrlSignerMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Сжатие JSON-ответов: списки постов/классов с текстами лекций ужимаются в разы.
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(users.router)
app.include_router(posts.router)
app.include_router(chats.router)
app.include_router(messages.router)
app.include_router(ws_router)
app.include_router(reactions.router)
app.include_router(uploads.router)
app.include_router(ai.router)
app.include_router(notifications.router)
app.include_router(push.router)
app.include_router(reports.router)
app.include_router(avatars.router)
app.include_router(assignments_router)
app.include_router(classes_router)
app.include_router(cohorts_router)
app.include_router(rating_router)
app.include_router(rag_router)

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


@app.get("/uploads/{filename:path}")
def serve_upload(
    filename: str,
    exp: int = Query(...),
    sig: str = Query(...),
):
    """SEC-1: файлы отдаются только по действующей подписанной ссылке.
    Подпись выдаёт бэкенд при возврате ресурса (сдача/эталон/обложка), поэтому
    получить её может лишь тот, кто вправе прочитать сам ресурс."""
    if not verify_signature(filename, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired file link")

    full = os.path.realpath(os.path.join(_uploads_root, filename))
    # Защита от path traversal (../): результат обязан лежать внутри uploads.
    if full != _uploads_root and not full.startswith(_uploads_root + os.sep):
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    disposition = "inline" if ext in _INLINE_EXTENSIONS else "attachment"
    return FileResponse(
        full,
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type=disposition,
    )


@app.get("/health")
def health():
    return {"status": "ok"}
