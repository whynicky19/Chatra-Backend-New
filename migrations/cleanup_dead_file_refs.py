"""Чистка БД от ссылок на файлы, которых нет в uploads/.

Файлы терялись при переносе проекта между машинами: строки в БД остались,
а бинарники — нет. Приложение на такие ссылки получает 404.

Обрабатывает:
  * submissions.file_url / file_urls
  * assignments.reference_solution_url
  * posts: списки files в JSON-теле

Запуск:
  ./venv/bin/python migrations/cleanup_dead_file_refs.py           # сухой прогон
  ./venv/bin/python migrations/cleanup_dead_file_refs.py --apply   # применить
"""
import json
import os
import sys
from urllib.parse import urlparse

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND, ".env"))

from db import SessionLocal
from models import (
    Assignment,
    Posts,
    Submission,
)

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
APPLY = "--apply" in sys.argv


def is_dead(url: str | None) -> bool:
    """True, если url указывает в /uploads/ и файла нет на диске.
    Чужие URL (D-ID и т.п.) не трогаем. Новые файлы в /uploads/r2/... живут в
    Cloudflare R2, а не на диске, — эта проверка их не касается."""
    if not url or not isinstance(url, str):
        return False
    path = urlparse(url.split("#")[0]).path
    if "/uploads/r2/" in path:
        return False
    if "/uploads/" not in path:
        return False
    name = os.path.basename(path)
    return not os.path.exists(os.path.join(UPLOAD_DIR, name))


def parse_url_list(raw) -> list | None:
    if not raw:
        return None
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


db = SessionLocal()
stats: dict[str, int] = {}


def bump(key: str):
    stats[key] = stats.get(key, 0) + 1


try:
    for sub in db.query(Submission).all():
        if is_dead(sub.file_url):
            print(f"submission {sub.id}: file_url мёртв -> NULL ({sub.file_url})")
            sub.file_url = None
            bump("submissions.file_url")
        urls = parse_url_list(sub.file_urls)
        if urls is not None:
            alive = [u for u in urls if not is_dead(str(u))]
            if len(alive) != len(urls):
                print(f"submission {sub.id}: file_urls {len(urls)} -> {len(alive)}")
                sub.file_urls = json.dumps(alive, ensure_ascii=False) if alive else None
                bump("submissions.file_urls")

    for a in db.query(Assignment).all():
        raw = a.reference_solution_url
        # reference_solution_url хранится либо одиночным URL, либо JSON-массивом
        # ["url1","url2"] (см. routers/assignments.py). Раньше is_dead() на строке
        # "[...]" всегда возвращал False, и мёртвые файлы внутри мультиэталона
        # никогда не вычищались. Разбираем оба формата.
        if raw and isinstance(raw, str) and raw.lstrip().startswith("["):
            urls = parse_url_list(raw)
            if urls is not None:
                alive = [u for u in urls if not is_dead(str(u))]
                if len(alive) != len(urls):
                    print(f"assignment {a.id}: reference_solution_url {len(urls)} -> {len(alive)}")
                    a.reference_solution_url = (
                        json.dumps(alive, ensure_ascii=False) if alive else None
                    )
                    bump("assignments.reference_solution_url")
        elif is_dead(raw):
            print(f"assignment {a.id}: reference_solution_url мёртв -> NULL")
            a.reference_solution_url = None
            bump("assignments.reference_solution_url")

    for post in db.query(Posts).filter(Posts.body.like('%"files"%')).all():
        try:
            body = json.loads(post.body)
        except Exception:
            continue
        files = body.get("files")
        if not isinstance(files, list):
            continue
        alive = [f for f in files if not is_dead(str(f))]
        if len(alive) != len(files):
            print(f"post {post.id} «{(post.title or '')[:50]}»: files {len(files)} -> {len(alive)}")
            body["files"] = alive
            post.body = json.dumps(body, ensure_ascii=False)
            bump("posts.files")

    print("\nИтого изменений:", json.dumps(stats, ensure_ascii=False, indent=2) if stats else "нет")
    if APPLY:
        db.commit()
        print("ПРИМЕНЕНО.")
    else:
        db.rollback()
        print("Сухой прогон — ничего не изменено. Запустите с --apply для применения.")
finally:
    db.close()
