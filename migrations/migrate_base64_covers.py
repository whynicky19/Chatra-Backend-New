"""Одноразовая миграция: base64-обложки из БД → файлы в uploads/.

Обрабатывает:
  * classes.cover_image с data-URI
  * legacy-посты, у которых в JSON-теле лежит cover_image с data-URI

Запуск (из каталога бэкенда):
  ./venv/bin/python migrations/migrate_base64_covers.py
"""
import json
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND, ".env"))

from db import SessionLocal
from models import Class, Posts
from services.image_storage import data_uri_to_file_url

db = SessionLocal()
try:
    classes_done = 0
    for cls in db.query(Class).filter(Class.cover_image.like("data:%")).all():
        url = data_uri_to_file_url(cls.cover_image)
        if url:
            cls.cover_image = url
            classes_done += 1
        else:
            print(f"Класс {cls.id}: обложка не декодировалась, оставлена как есть")
    db.commit()
    print(f"Классы: сконвертировано обложек {classes_done}")

    posts_done = 0
    for post in db.query(Posts).filter(Posts.body.like('%"cover_image":"data:%')).all():
        try:
            body = json.loads(post.body)
        except Exception:
            print(f"Пост {post.id}: тело не является JSON, пропуск")
            continue
        cover = body.get("cover_image")
        if not isinstance(cover, str) or not cover.startswith("data:"):
            continue
        url = data_uri_to_file_url(cover)
        if url:
            body["cover_image"] = url
            post.body = json.dumps(body, ensure_ascii=False)
            posts_done += 1
        else:
            print(f"Пост {post.id}: обложка не декодировалась, оставлена как есть")
    db.commit()
    print(f"Посты: сконвертировано обложек {posts_done}")
finally:
    db.close()
