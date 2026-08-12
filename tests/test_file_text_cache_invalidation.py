"""Регрессия: R2-ключи строятся из человекочитаемого имени (не UUID) и
освобождаются после удаления файла — следующая загрузка с тем же именем
может получить тот же ключ. _file_text_cache в routers/uploads.py раньше не
инвалидировался при удалении, поэтому GET /upload/utils/file-text мог отдать
текст СТАРОГО удалённого файла новому файлу под тем же путём."""
from routers.uploads import _file_text_cache
import services.file_cleanup as file_cleanup


class _FakeStorage:
    def __init__(self, existing_keys):
        self._existing = set(existing_keys)

    def delete(self, key):
        if key in self._existing:
            self._existing.discard(key)
            return True
        return False


def test_delete_upload_file_invalidates_file_text_cache(monkeypatch):
    key = "materials/notes.pdf"
    file_path = f"r2/{key}"
    _file_text_cache[file_path] = "старый текст удалённого файла"

    fake_storage = _FakeStorage({key})
    monkeypatch.setattr(file_cleanup, "get_storage_service", lambda: fake_storage)

    url = f"http://localhost:8000/api/uploads/r2/{key}?exp=1&sig=aa"
    assert file_cleanup.delete_upload_file(url) is True

    assert file_path not in _file_text_cache


def test_delete_upload_file_does_not_touch_cache_when_nothing_was_deleted(monkeypatch):
    key = "materials/notes.pdf"
    file_path = f"r2/{key}"
    _file_text_cache[file_path] = "актуальный текст"

    fake_storage = _FakeStorage(set())  # уже удалён/не существует
    monkeypatch.setattr(file_cleanup, "get_storage_service", lambda: fake_storage)

    url = f"http://localhost:8000/api/uploads/r2/{key}?exp=1&sig=aa"
    assert file_cleanup.delete_upload_file(url) is False

    assert _file_text_cache.get(file_path) == "актуальный текст"
