"""Абстракция объектного хранилища для новых загрузок (Cloudflare R2).

Старые файлы в uploads/ этот модуль не касается — они по-прежнему читаются и
удаляются напрямую с диска (см. main.py и services/file_cleanup.py). Новый код
должен получать сервис через get_storage_service() и не создавать
R2StorageService напрямую, кроме тестов.
"""
from services.storage.base import StorageError, StorageService
from services.storage.r2_storage import R2StorageService

_service: StorageService | None = None


def get_storage_service() -> StorageService:
    global _service
    if _service is None:
        _service = R2StorageService.from_env()
    return _service


def reset_storage_service() -> None:
    """Для тестов: сбрасывает синглтон, чтобы следующий вызов пере-читал env."""
    global _service
    _service = None


__all__ = ["StorageError", "StorageService", "R2StorageService", "get_storage_service", "reset_storage_service"]
