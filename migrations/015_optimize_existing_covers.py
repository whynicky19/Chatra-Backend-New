"""Разовый бэкфилл: пересжимает уже существующие обложки классов (см.
014_classes_cover_thumbnail.sql + services/image_processing.py) — старые
обложки были залиты в R2 как есть, без ресайза/WebP/миниатюры, и продолжат
грузиться медленно, пока их не пересохранить через новый пайплайн.

Для каждого класса с cover_image, указывающим на объект в R2 (не data-URI —
те уже обработаны migrate_base64_covers.py, и не сторонний URL):
  1. скачивает исходные байты напрямую из R2 (по ключу, в обход HTTP/подписи);
  2. прогоняет через services.image_processing.process_cover_image;
  3. заливает новый cover_image (WebP, <=1600px) и cover_thumbnail (<=480px)
     под новыми ключами (см. StorageService.build_key — коллизий не будет);
  4. обновляет classes.cover_image/cover_thumbnail на новые URL;
  5. best-effort удаляет старый объект (не роняет миграцию при сбое).

Запуск (из каталога бэкенда, после применения 014_classes_cover_thumbnail.sql):
  ./venv/bin/python migrations/015_optimize_existing_covers.py
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND, ".env"))

from db import SessionLocal
from models import Class
from services.image_processing import process_cover_image
from services.storage import StorageError, get_storage_service

_CONTENT_TYPE_BY_EXT = {"webp": "image/webp", "jpg": "image/jpeg"}
_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _r2_key_from_url(url: str, app_base_url: str, public_base_url: str | None) -> str | None:
    """Достаёт ключ объекта R2 из URL — как из проксированной формы
    "{APP_BASE_URL}/uploads/r2/<key>", так и из прямой публичной
    "{R2_PUBLIC_BASE_URL}/<key>", если она уже настроена."""
    if not url:
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    marker = "/uploads/r2/"
    idx = path.find(marker)
    if idx != -1:
        return path[idx + len(marker):].strip("/") or None
    if public_base_url and path.startswith(public_base_url.rstrip("/") + "/"):
        return path[len(public_base_url.rstrip("/")) + 1:].strip("/") or None
    return None


def main() -> None:
    storage = get_storage_service()
    app_base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
    public_base_url = os.getenv("R2_PUBLIC_BASE_URL")

    db = SessionLocal()
    done = skipped = failed = 0
    try:
        classes = db.query(Class).filter(
            Class.cover_image.isnot(None),
            ~Class.cover_image.like("data:%"),
        ).all()
        for cls in classes:
            old_key = _r2_key_from_url(cls.cover_image, app_base_url, public_base_url)
            if not old_key:
                skipped += 1
                continue
            try:
                obj = storage.get_object(old_key)
                if obj is None:
                    print(f"Класс {cls.id}: объект {old_key} не найден в R2, пропуск")
                    skipped += 1
                    continue

                main_bytes, main_ext, thumb_bytes, thumb_ext = process_cover_image(obj.body)

                main_key = storage.build_key("materials/covers", f"cover.{main_ext}")
                cover_url = storage.upload(
                    main_bytes, main_key, _CONTENT_TYPE_BY_EXT.get(main_ext, "application/octet-stream"),
                    cache_control=_CACHE_CONTROL,
                )
                thumb_key = storage.build_key("materials/covers/thumbnails", f"cover.{thumb_ext}")
                thumbnail_url = storage.upload(
                    thumb_bytes, thumb_key, _CONTENT_TYPE_BY_EXT.get(thumb_ext, "application/octet-stream"),
                    cache_control=_CACHE_CONTROL,
                )
            except StorageError as e:
                print(f"Класс {cls.id}: сбой R2 при обработке {old_key}: {e}")
                failed += 1
                continue
            except Exception as e:  # noqa: BLE001 — битая/нечитаемая картинка не должна ронять весь прогон
                print(f"Класс {cls.id}: не удалось обработать картинку {old_key}: {e}")
                failed += 1
                continue

            cls.cover_image = cover_url
            cls.cover_thumbnail = thumbnail_url
            db.commit()
            done += 1

            try:
                storage.delete(old_key)
            except StorageError as e:
                print(f"Класс {cls.id}: старый объект {old_key} не удалён (не критично): {e}")

        print(f"Готово: обработано {done}, пропущено {skipped}, ошибок {failed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
