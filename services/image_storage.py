"""Конвертация base64 data-URI картинок обложек классов в файлы в R2.

Обложки классов исторически сохранялись как data-URI прямо в БД и уезжали
в каждый списочный ответ (~100-150 КБ на класс). Здесь они один раз
декодируются, сжимаются (ресайз + WebP — см. services/image_processing.py)
и заливаются в R2 (materials/covers/...), а в БД остаётся короткий URL.
"""
import base64
import logging
import re

from services.image_processing import process_cover_image
from services.storage import StorageError, get_storage_service

logger = logging.getLogger(__name__)

_DATA_URI_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp|gif);base64,", re.IGNORECASE)
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_CONTENT_TYPE_BY_EXT = {"webp": "image/webp", "jpg": "image/jpeg"}
# Обложки не содержат ничего секретного (их и так видит любой в классе) —
# get_url() отдаёт их напрямую из R2/CDN в обход HMAC-подписи и прокси
# (см. services/storage/base.py: PUBLIC_KEY_PREFIXES). Год — потому что имя
# ключа уникально на каждую загрузку (UUID), переиспользования URL под
# другим контентом не бывает, значит кэш можно не инвалидировать вообще.
_COVER_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _decode_data_uri(data_uri: str) -> bytes | None:
    if not isinstance(data_uri, str):
        return None
    m = _DATA_URI_RE.match(data_uri)
    if not m:
        return None
    try:
        raw = base64.b64decode(data_uri[m.end():], validate=False)
    except Exception:
        logger.warning("Не удалось декодировать data-URI картинку (len=%s)", len(data_uri))
        return None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None
    return raw


def _process_and_upload(raw: bytes) -> tuple[str, str] | None:
    """Сжимает картинку (ресайз ≤1600px + WebP) и грузит основной файл и
    миниатюру (≤480px) в R2. Возвращает (cover_url, thumbnail_url) или None
    при сбое обработки/загрузки — вызывающий код в этом случае оставляет
    исходное значение как есть."""
    try:
        main_bytes, main_ext, thumb_bytes, thumb_ext = process_cover_image(raw)
    except Exception:
        logger.exception("Не удалось обработать картинку обложки (decode/resize/encode)")
        return None

    storage = get_storage_service()
    try:
        main_key = storage.build_key("materials/covers", f"cover.{main_ext}")
        cover_url = storage.upload(
            main_bytes, main_key, _CONTENT_TYPE_BY_EXT.get(main_ext, "application/octet-stream"),
            cache_control=_COVER_CACHE_CONTROL,
        )
        thumb_key = storage.build_key("materials/covers/thumbnails", f"cover.{thumb_ext}")
        thumbnail_url = storage.upload(
            thumb_bytes, thumb_key, _CONTENT_TYPE_BY_EXT.get(thumb_ext, "application/octet-stream"),
            cache_control=_COVER_CACHE_CONTROL,
        )
    except StorageError:
        logger.exception("Не удалось сохранить картинку обложки в R2")
        return None
    return cover_url, thumbnail_url


def data_uri_to_file_url(data_uri: str) -> str | None:
    """Сохраняет data-URI картинку (сжатую) в R2 и возвращает URL основного
    файла. Возвращает None, если строка не является поддерживаемой картинкой
    или обработка не удалась — вызывающий код в этом случае оставляет
    значение как есть."""
    raw = _decode_data_uri(data_uri)
    if raw is None:
        return None
    result = _process_and_upload(raw)
    return result[0] if result else None


def convert_cover_if_data_uri(value: str | None) -> str | None:
    """Если value — data-URI картинка, конвертирует в файл и возвращает URL;
    иначе возвращает value без изменений."""
    if value and value.startswith("data:"):
        url = data_uri_to_file_url(value)
        if url:
            return url
    return value


def convert_cover_with_thumbnail(value: str | None) -> tuple[str | None, str | None]:
    """Как convert_cover_if_data_uri, но также возвращает URL миниатюры
    (≤480px) для карточек списка классов. Для значений, не являющихся
    data-URI (уже готовый URL, None), миниатюра не трогается — вызывающий
    код должен сохранить прежнее значение cover_thumbnail в этом случае."""
    if not value or not value.startswith("data:"):
        return value, None
    raw = _decode_data_uri(value)
    if raw is None:
        return value, None
    result = _process_and_upload(raw)
    if not result:
        return value, None
    return result
