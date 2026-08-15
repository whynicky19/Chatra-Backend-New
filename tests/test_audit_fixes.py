"""Регрессии на остальные находки полного аудита бэкенда.

1. Обложки классов, отдаваемые напрямую из R2/CDN (R2_PUBLIC_BASE_URL), не
   распознавались как объекты хранилища и никогда не удалялись при замене
   обложки/удалении класса — утечка файлов, растущая с каждой генерацией.
2. Две параллельные выдачи OTP-кода оставляли две строки email_codes, после
   чего verify_code падал MultipleResultsFound (500) до истечения кодов.
"""
import services.file_cleanup as file_cleanup
from services.otp import issue_code, verify_code


class _FakeStorage:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return True


def _patch_storage(monkeypatch) -> _FakeStorage:
    storage = _FakeStorage()
    monkeypatch.setattr(file_cleanup, "get_storage_service", lambda: storage)
    return storage


def test_public_cdn_cover_url_is_deleted_from_storage(monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example.com")
    storage = _patch_storage(monkeypatch)

    url = "https://cdn.example.com/materials/covers/cover_abc123.webp"
    assert file_cleanup.delete_upload_file(url) is True
    assert storage.deleted == ["materials/covers/cover_abc123.webp"]


def test_public_cdn_url_with_trailing_slash_in_base(monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example.com/")
    storage = _patch_storage(monkeypatch)

    url = "https://cdn.example.com/materials/covers/thumbnails/cover_x.webp?v=2"
    assert file_cleanup.delete_upload_file(url) is True
    assert storage.deleted == ["materials/covers/thumbnails/cover_x.webp"]


def test_proxy_r2_url_still_deleted(monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example.com")
    storage = _patch_storage(monkeypatch)

    url = "http://localhost:8000/api/uploads/r2/submissions/otchet.pdf?exp=1&sig=2"
    assert file_cleanup.delete_upload_file(url) is True
    assert storage.deleted == ["submissions/otchet.pdf"]


def test_foreign_host_is_not_treated_as_own_storage(monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://cdn.example.com")
    storage = _patch_storage(monkeypatch)

    # Похожий, но чужой хост — ничего не трогаем.
    assert file_cleanup.delete_upload_file("https://cdn.example.com.evil.tld/a/b.webp") is False
    assert storage.deleted == []


def test_verify_code_survives_duplicate_rows(db_session):
    """Гонка двух /auth/resend-verification: обе транзакции успевают удалить
    старую строку до вставки своей, в таблице остаются две. Проверка кода
    обязана работать по последнему выданному, а не падать 500."""
    from datetime import timedelta

    import models
    from utils.time import utcnow

    email = "dup@example.com"
    # Строка «проигравшего» запроса гонки: выдана чуть раньше, её кода в
    # последнем письме уже нет.
    db_session.add(models.EmailCode(
        email=email, org_type="university", purpose="verify",
        code_hash="stale-hash", attempts=0,
        created_at=utcnow() - timedelta(seconds=5),
        expires_at=utcnow() + timedelta(minutes=10),
    ))
    db_session.commit()

    latest = issue_code(db_session, email, "university", "verify")
    # issue_code удаляет предыдущие строки, поэтому дубль возвращаем вручную —
    # ровно то состояние, которое оставляет гонка.
    db_session.add(models.EmailCode(
        email=email, org_type="university", purpose="verify",
        code_hash="stale-hash", attempts=0,
        created_at=utcnow() - timedelta(seconds=5),
        expires_at=utcnow() + timedelta(minutes=10),
    ))
    db_session.commit()
    assert db_session.query(models.EmailCode).filter(models.EmailCode.email == email).count() == 2

    # До фикса здесь падало MultipleResultsFound (500 на подтверждении email).
    assert verify_code(db_session, email, "university", "verify", latest) is True
