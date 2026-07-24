"""Конвертация base64 data-URI картинок в файлы в uploads/.

Обложки классов исторически сохранялись как data-URI прямо в БД и уезжали
в каждый списочный ответ (~100-150 КБ на класс). Здесь они один раз
декодируются в файл, а в БД остаётся короткий URL.
"""
import base64
import logging
import re

from services.storage import StorageError, get_storage_service

logger = logging.getLogger(__name__)

_DATA_URI_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp|gif);base64,", re.IGNORECASE)
_EXT_BY_TYPE = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}
_MIME_BY_TYPE = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def data_uri_to_file_url(data_uri: str) -> str | None:
    """Сохраняет data-URI картинку в uploads/ и возвращает её URL.
    Возвращает None, если строка не является поддерживаемой картинкой
    или не декодируется — в этом случае вызывающий код оставляет значение как есть."""
    if not isinstance(data_uri, str):
        return None
    m = _DATA_URI_RE.match(data_uri)
    if not m:
        return None
    ext = _EXT_BY_TYPE[m.group(1).lower()]
    try:
        raw = base64.b64decode(data_uri[m.end():], validate=False)
    except Exception:
        logger.warning("Не удалось декодировать data-URI картинку (len=%s)", len(data_uri))
        return None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None

    storage = get_storage_service()
    key = storage.build_key("covers", ext)
    try:
        return storage.upload(raw, key, _MIME_BY_TYPE[m.group(1).lower()])
    except StorageError:
        logger.exception("Не удалось сохранить картинку обложки (key=%s)", key)
        return None


def convert_cover_if_data_uri(value: str | None) -> str | None:
    """Если value — data-URI картинка, конвертирует в файл и возвращает URL;
    иначе возвращает value без изменений."""
    if value and value.startswith("data:"):
        url = data_uri_to_file_url(value)
        if url:
            return url
    return value
