"""BE-10: удаление файлов из локального хранилища uploads/.

Раньше при удалении сдачи/задания/класса запись в БД пропадала, а сами файлы
навсегда оставались на диске. Здесь — безопасное best-effort удаление: только
внутри UPLOAD_DIR (защита от path traversal), сбои логируются и не роняют
основную операцию (удаление в БД важнее, чем очистка диска).
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
_UPLOADS_ROOT = os.path.realpath(_UPLOAD_DIR)


def _filename_from_url(url: str) -> str | None:
    if not url:
        return None
    # URL вида {APP_BASE_URL}/uploads/<uuid>.<ext>[?exp=..&sig=..]
    path = url.split("?", 1)[0].split("#", 1)[0]
    marker = "/uploads/"
    idx = path.find(marker)
    tail = path[idx + len(marker):] if idx != -1 else path
    tail = tail.strip("/")
    return tail or None


def delete_upload_file(url: str) -> bool:
    """Удаляет один файл по его URL/пути. True — файл удалён."""
    name = _filename_from_url(url)
    if not name:
        return False
    full = os.path.realpath(os.path.join(_UPLOADS_ROOT, name))
    # Только внутри uploads/ — не даём удалить что-то по ../ в имени.
    if full != _UPLOADS_ROOT and not full.startswith(_UPLOADS_ROOT + os.sep):
        logger.warning("file_cleanup: путь вне uploads, пропуск: %s", url)
        return False
    try:
        os.remove(full)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("file_cleanup: не удалось удалить %s (%s)", full, e)
        return False


def delete_submission_files(submission) -> int:
    """Удаляет все файлы сдачи (file_url + file_urls JSON). Возвращает число
    фактически удалённых файлов."""
    urls: list[str] = []
    if getattr(submission, "file_url", None):
        urls.append(submission.file_url)
    if getattr(submission, "file_urls", None):
        try:
            parsed = json.loads(submission.file_urls)
            if isinstance(parsed, list):
                urls.extend(u for u in parsed if u)
        except (ValueError, TypeError):
            pass
    removed = 0
    for u in set(urls):
        if delete_upload_file(u):
            removed += 1
    return removed
