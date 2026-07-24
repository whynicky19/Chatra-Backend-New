"""Интерфейс объектного хранилища для новых файлов."""
from abc import ABC, abstractmethod
from uuid import uuid4


class StorageError(Exception):
    """Неустранимый сбой хранилища (после исчерпания ретраев)."""


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

    @staticmethod
    def build_key(prefix: str, ext: str) -> str:
        """Генерирует непредсказуемый ключ вида '<prefix>/<uuid>.<ext>'."""
        ext = ext.lstrip(".").lower()
        name = f"{uuid4().hex}.{ext}" if ext else uuid4().hex
        return f"{prefix}/{name}" if prefix else name
