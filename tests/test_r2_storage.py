"""Юнит-тесты R2StorageService: upload/delete/exists/replace/get_url и ретраи.
boto3-клиент замокан — реального обращения к Cloudflare R2 нет."""
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from services.storage.base import StorageError
from services.storage import r2_storage
from services.storage.r2_storage import R2StorageService


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(r2_storage.time, "sleep", lambda _seconds: None)


def _service(public_base_url=None):
    svc = R2StorageService(
        bucket="test-bucket",
        endpoint_url="https://example.r2.cloudflarestorage.com",
        access_key_id="key",
        secret_access_key="secret",
        public_base_url=public_base_url,
        app_base_url="http://localhost:8000",
    )
    svc._client = MagicMock()
    return svc


def _client_error(status: int) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadObject",
    )


def test_get_url_without_public_base_uses_signed_proxy():
    svc = _service()
    assert svc.get_url("uploads/abc.pdf") == "http://localhost:8000/uploads/r2/uploads/abc.pdf"


def test_get_url_with_public_base_is_direct():
    svc = _service(public_base_url="https://cdn.example.com")
    assert svc.get_url("uploads/abc.pdf") == "https://cdn.example.com/uploads/abc.pdf"


def test_upload_calls_put_object_and_returns_url():
    svc = _service()
    url = svc.upload(b"hello", "uploads/x.txt", "text/plain")
    svc._client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="uploads/x.txt", Body=b"hello", ContentType="text/plain",
    )
    assert url == "http://localhost:8000/uploads/r2/uploads/x.txt"


def test_exists_true_when_head_object_succeeds():
    svc = _service()
    svc._client.head_object.return_value = {}
    assert svc.exists("uploads/x.txt") is True


def test_exists_false_on_404():
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    assert svc.exists("uploads/missing.txt") is False


def test_delete_returns_false_when_missing():
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    assert svc.delete("uploads/missing.txt") is False
    svc._client.delete_object.assert_not_called()


def test_delete_removes_existing_file():
    svc = _service()
    svc._client.head_object.return_value = {}
    assert svc.delete("uploads/x.txt") is True
    svc._client.delete_object.assert_called_once_with(Bucket="test-bucket", Key="uploads/x.txt")


def test_replace_overwrites_same_key():
    svc = _service()
    svc.replace(b"new content", "uploads/x.txt", "text/plain")
    svc._client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="uploads/x.txt", Body=b"new content", ContentType="text/plain",
    )


def test_upload_retries_on_transient_error_then_succeeds():
    svc = _service()
    svc._client.put_object.side_effect = [_client_error(503), {}]
    svc.upload(b"data", "uploads/x.txt", "text/plain")
    assert svc._client.put_object.call_count == 2


def test_upload_raises_storage_error_after_exhausting_retries():
    svc = _service()
    svc._client.put_object.side_effect = _client_error(503)
    with pytest.raises(StorageError):
        svc.upload(b"data", "uploads/x.txt", "text/plain")
    assert svc._client.put_object.call_count == 3


def test_upload_does_not_retry_on_client_error_4xx():
    svc = _service()
    svc._client.put_object.side_effect = _client_error(403)
    with pytest.raises(StorageError):
        svc.upload(b"data", "uploads/x.txt", "text/plain")
    assert svc._client.put_object.call_count == 1


def test_build_key_uses_original_filename_and_category():
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    key = svc.build_key("submissions", "Домашнее задание.docx")
    assert key == "submissions/Домашнее задание.docx"


def test_build_key_strips_path_and_unsafe_chars():
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    key = svc.build_key("attachments", "../../etc/passwd:evil<name>.txt")
    assert key.startswith("attachments/")
    assert "/" not in key[len("attachments/"):]
    assert ".." not in key
    assert ":" not in key and "<" not in key and ">" not in key


def test_build_key_appends_numeric_suffix_on_collision():
    svc = _service()
    # Первые два кандидата уже заняты, третий свободен.
    svc._client.head_object.side_effect = [
        {}, {}, _client_error(404),
    ]
    key = svc.build_key("materials", "report.pdf")
    assert key == "materials/report_2.pdf"


def test_build_key_falls_back_to_uuid_after_exhausting_suffixes():
    svc = _service()
    svc._client.head_object.side_effect = [{}] * 25  # всегда занято
    key = svc.build_key("materials", "report.pdf")
    assert key.startswith("materials/report_") and key.endswith(".pdf")
    assert key.count("_") >= 1
    # UUID-хвост, а не порядковый номер из основного диапазона (1..20)
    tail = key[len("materials/report_"):-len(".pdf")]
    assert tail not in {str(i) for i in range(1, 21)}


def test_build_key_no_extension_when_none_present():
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    key = svc.build_key("attachments", "README")
    assert key == "attachments/README"
