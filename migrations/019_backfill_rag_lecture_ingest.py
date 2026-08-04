"""Миграция 019: разовый бэкфилл RAG-индекса для лекций, созданных/не
редактировавшихся ДО того, как services/rag_ingest.py стал подключаться к
create_new_post/update_post (см. migrations/018_rag_lecture_ingest.sql).

Без этого скрипта такие лекции никогда не попадут в rag_chunks сами —
ingest_lecture запускается только при создании/правке поста. Идемпотентно:
ingest_lecture сам решает по content_hash, что уже проиндексировано, повторный
запуск безопасен.

    DATABASE_URL=postgresql://... python migrations/019_backfill_rag_lecture_ingest.py
"""
import asyncio
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND, ".env"))

from db import SessionLocal
from crud.posts import _LECTURE_TITLE_RE
from models import Posts
from services.rag_ingest import ingest_lecture


async def main() -> None:
    db = SessionLocal()
    try:
        lecture_posts = [
            p for p in db.query(Posts).all() if _LECTURE_TITLE_RE.match(p.title or "")
        ]
        print(f"Найдено лекций: {len(lecture_posts)}")
        for i, post in enumerate(lecture_posts, 1):
            print(f"[{i}/{len(lecture_posts)}] ingest_lecture(post_id={post.id}, title={post.title!r})")
            try:
                await ingest_lecture(db, post.id)
            except Exception as e:
                print(f"  ОШИБКА: {e}")
        print("Готово.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
