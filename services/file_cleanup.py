"""BE-10: удаление файлов, на которые ссылается БД (локальный uploads/ и R2).

Раньше при удалении сдачи/задания/класса запись в БД пропадала, а сами файлы
навсегда оставались на диске. Здесь — безопасное best-effort удаление: только
внутри UPLOAD_DIR для локальных файлов (защита от path traversal) или через
StorageService для новых файлов в R2 (см. services/storage/); сбои логируются
и не роняют основную операцию (удаление в БД важнее, чем очистка хранилища).
"""
import json
import logging
import os
import re

from services.storage import StorageError, get_storage_service

logger = logging.getLogger(__name__)

_UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
_UPLOADS_ROOT = os.path.realpath(_UPLOAD_DIR)

_R2_MARKER = "/uploads/r2/"

# Файлы задания (в отличие от лекций, у которых свой JSON-массив body["files"])
# живут прямо ВНУТРИ текста description как ссылки вида "<url>#<имя>" —
# зеркалит fileUrlRe из lib/screens/classes/class_detail_utils.dart (Flutter)
# и SIGNED_UPLOAD_URL_RE из composables/useAttachments.ts (Nuxt), которыми
# клиенты вставляют и парсят эти ссылки. Нужен свой парсер здесь, иначе при
# удалении/редактировании задания эти файлы никогда не подчищаются в
# хранилище (BE-10 их просто не видел).
_DESCRIPTION_FILE_URL_RE = re.compile(
    r'https?://[^\s/]+/[^\s"<>]*?\.'
    r'(?:pdf|docx?|pptx?|xlsx?|txt|md|csv|rtf|png|jpe?g|gif|webp|mp3|wav|m4a|webm|ogg|mp4)'
    r'(?:\?[^\s"<>]*)?(?:#[^\s"<>]*)?',
    re.IGNORECASE,
)


def description_file_urls(description: str | None) -> set[str]:
    """Все ссылки на файлы, встроенные в текст description задания."""
    if not description:
        return set()
    return {m.group(0) for m in _DESCRIPTION_FILE_URL_RE.finditer(description)}


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


def _r2_key_from_url(url: str) -> str | None:
    if not url:
        return None
    path = url.split("?", 1)[0].split("#", 1)[0]
    idx = path.find(_R2_MARKER)
    if idx == -1:
        return None
    key = path[idx + len(_R2_MARKER):].strip("/")
    return key or None


def delete_upload_file(url: str) -> bool:
    """Удаляет один файл по его URL/пути. True — файл удалён."""
    r2_key = _r2_key_from_url(url)
    if r2_key is not None:
        try:
            return get_storage_service().delete(r2_key)
        except StorageError as e:
            logger.warning("file_cleanup: не удалось удалить из R2 %s (%s)", r2_key, e)
            return False

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


def delete_class_files(klass) -> int:
    """Удаляет обложку класса (cover_image/cover_thumbnail). Только файлы
    самого класса — задания и сдачи класс не удаляет (class_id у Assignment
    не FK, они переживают удаление класса)."""
    urls = {u for u in (getattr(klass, "cover_image", None), getattr(klass, "cover_thumbnail", None)) if u}
    return sum(1 for u in urls if delete_upload_file(u))


def delete_assignment_files(assignment) -> int:
    """Удаляет референсное решение задания + файлы самого задания (встроены
    в description, см. description_file_urls) + файлы всех сдач (вызывается
    перед удалением задания, чьи сдачи каскадно уходят вместе с ним)."""
    urls: set[str] = set(_parse_reference_urls_local(getattr(assignment, "reference_solution_url", None)))
    urls |= description_file_urls(getattr(assignment, "description", None))
    removed = sum(1 for u in urls if delete_upload_file(u))
    for submission in getattr(assignment, "submissions", None) or []:
        removed += delete_submission_files(submission)
    return removed


def _parse_reference_urls_local(raw: str | None) -> list[str]:
    """Копия crud.assignments._parse_reference_urls — reference_solution_url
    хранит либо один URL, либо JSON-массив URL. Своя копия здесь, а не
    импорт: crud/assignments.py импортирует ИЗ этого модуля (delete_upload_file
    и т.д.), обратный импорт дал бы циклическую зависимость."""
    if not raw:
        return []
    if raw.startswith('['):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [u for u in parsed if u]
        except Exception:
            pass
    return [raw]


def _post_cover_url(body: str | None) -> str | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    cover = parsed.get("cover_image")
    if not cover or not isinstance(cover, str) or cover.startswith("data:"):
        return None
    return cover


def _post_attachment_urls(body: str | None) -> list[str]:
    """Файлы, прикреплённые к лекции (body["files"]) — те же источники, что
    инжестит services/rag_ingest.py::_parse_lecture_files."""
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    files = parsed.get("files")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and f.strip() and not f.startswith("data:")]


def delete_post_files(post) -> int:
    """Удаляет обложку поста/лекции (если она была сконвертирована в файл R2,
    см. routers/posts.py: _convert_body_cover — легаси data-URI обложки
    отдельного файла не имеют, чистить нечего) и все прикреплённые файлы
    лекции (body["files"])."""
    body = getattr(post, "body", None)
    urls = {u for u in ([_post_cover_url(body)] + _post_attachment_urls(body)) if u}
    return sum(1 for u in urls if delete_upload_file(u))


def delete_replaced_post_cover(old_body: str | None, new_body: str | None) -> bool:
    """При редактировании поста — удаляет старую обложку, если она была
    файлом в R2/uploads и заменена на другую (или пост остался без обложки)."""
    old_cover = _post_cover_url(old_body)
    if old_cover and old_cover != _post_cover_url(new_body):
        return delete_upload_file(old_cover)
    return False


def delete_user_files(user) -> int:
    """Удаляет все файлы, привязанные к пользователю: его сдачи, посты
    (обложки лекций), файлы созданных им заданий (референсы, варианты, сдачи
    учеников по ним) и обложки созданных им классов — всё это каскадно
    уходит вместе с аккаунтом (см. models.py: cascade="all, delete-orphan")."""
    removed = 0
    for submission in getattr(user, "submissions", None) or []:
        removed += delete_submission_files(submission)
    for post in getattr(user, "posts", None) or []:
        removed += delete_post_files(post)
    for assignment in getattr(user, "assignments_created", None) or []:
        removed += delete_assignment_files(assignment)
    for klass in getattr(user, "classes_created", None) or []:
        removed += delete_class_files(klass)
    return removed
