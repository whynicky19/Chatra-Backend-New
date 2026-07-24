"""Интерфейс объектного хранилища для новых файлов.

Структура хранилища — по типу содержимого, а не одна общая папка (см.
CATEGORIES). Ключ объекта строится StorageService.build_key(): '<category>/
<человекочитаемое-имя>[.<ext>]', с оригинальным именем файла вместо
случайного UUID и авто-суффиксом при конфликте имён (см. docstring build_key).
"""
import os
import re
from abc import ABC, abstractmethod
from uuid import uuid4

# Категории верхнего уровня для организации файлов в R2. Внутри категории
# допустимы дополнительные подпапки (например "lectures/slides/<batch>") —
# это тоже "автоматически формируемый путь", просто более специфичный.
CATEGORIES = {"avatars", "lectures", "materials", "assignments", "submissions", "attachments"}

_UNSAFE_CHARS_RE = re.compile(r"[^\w\-. ]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_STEM_LENGTH = 150
_MAX_COLLISION_ATTEMPTS = 20


class StorageError(Exception):
    """Неустранимый сбой хранилища (после исчерпания ретраев)."""


def _sanitize_filename(original_filename: str) -> tuple[str, str]:
    """Возвращает (stem, ext) — безопасные для ключа R2 и для заголовка
    Content-Disposition: без разделителей пути, управляющих символов и т.п.
    Юникод (кириллица) сохраняется — файл должен остаться понятным юзеру."""
    base = os.path.basename((original_filename or "").strip())
    stem, ext = os.path.splitext(base)
    ext = re.sub(r"[^A-Za-z0-9]", "", ext.lstrip(".")).lower()
    stem = _UNSAFE_CHARS_RE.sub("_", stem)
    stem = _WHITESPACE_RE.sub(" ", stem).strip(" ._")
    stem = stem[:_MAX_STEM_LENGTH] or "file"
    return stem, ext


class StorageService(ABC):
    @abstractmethod
    def upload(self, content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        """Загружает файл под указанным ключом. Возвращает URL для сохранения в БД."""

    @abstractmethod
    def replace(self, content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        """Перезаписывает файл по существующему ключу. Возвращает URL."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Удаляет файл. True — файл был удалён, False — его и так не было."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Проверяет наличие файла в хранилище."""

    @abstractmethod
    def get_url(self, key: str) -> str:
        """Возвращает URL для отдачи файла клиенту (см. docstring в r2_storage.py)."""

    def build_key(self, category: str, original_filename: str) -> str:
        """Строит ключ '<category>/<имя>[.<ext>]', сохраняя оригинальное имя
        файла (а не случайный UUID), чтобы Content-Disposition при скачивании
        показывал юзеру понятное имя. При конфликте имени внутри категории
        добавляет числовой суффикс (_1, _2, ...), а после 20 неудачных попыток —
        короткий UUID-хвост, чтобы загрузка никогда не падала из-за коллизии."""
        category = category.strip("/")
        stem, ext = _sanitize_filename(original_filename)
        suffix = f".{ext}" if ext else ""

        candidate = f"{category}/{stem}{suffix}"
        if not self.exists(candidate):
            return candidate
        for i in range(1, _MAX_COLLISION_ATTEMPTS + 1):
            candidate = f"{category}/{stem}_{i}{suffix}"
            if not self.exists(candidate):
                return candidate
        return f"{category}/{stem}_{uuid4().hex[:8]}{suffix}"
