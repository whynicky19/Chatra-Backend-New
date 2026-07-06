from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from starlette.staticfiles import StaticFiles
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from db import Base, engine, get_engine
from models import Base
from routers import auth, admin, users, posts, chats, messages, reactions, uploads, ai, avatars, notifications
from routers.assignments import router as assignments_router
from routers.classes import router as classes_router, rating_router
from routers.cohorts import router as cohorts_router
from routers.rag import router as rag_router
from websocket import router as ws_router
from sqlalchemy import text
from services.deadline_checker import deadline_checker_loop

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

def _ensure_schemas():
    if not str(engine.url).startswith("postgresql"):
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS university"))
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS school"))
            conn.commit()
        for org in ["university", "school"]:
            Base.metadata.create_all(bind=get_engine(org))
        logging.info("Schemas ready: university, school")
    except Exception as e:
        logging.warning(f"Schema init skipped: {e}")

_ensure_schemas()

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
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Chatra API", version="3.0", lifespan=lifespan)

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

class UploadsStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["X-Content-Type-Options"] = "nosniff"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in _INLINE_EXTENSIONS:
            response.headers["Content-Disposition"] = "attachment"
        return response

app.mount("/uploads", UploadsStaticFiles(directory=_upload_dir), name="uploads")


@app.get("/health")
def health():
    return {"status": "ok"}
