"""Юнит-тесты R2StorageService: upload/delete/exists/replace/get_url и ретраи.
boto3-клиент замокан — реального обращения к Cloudflare R2 нет."""
import io
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
    assert svc.get_url("uploads/abc.pdf") == "http://localhost:8000/api/uploads/r2/uploads/abc.pdf"


def test_get_url_with_public_base_is_direct_for_public_category():
    svc = _service(public_base_url="https://cdn.example.com")
    assert svc.get_url("materials/covers/x.webp") == "https://cdn.example.com/materials/covers/x.webp"
    assert (
        svc.get_url("materials/covers/thumbnails/x.webp")
        == "https://cdn.example.com/materials/covers/thumbnails/x.webp"
    )


def test_get_url_with_public_base_still_proxies_private_categories():
    # submissions/assignments и т.п. остаются приватными (SEC-1) даже если
    # R2_PUBLIC_BASE_URL включён ради обложек — паблик-домен не должен
    # случайно рассекретить чужие сдачи.
    svc = _service(public_base_url="https://cdn.example.com")
    assert svc.get_url("submissions/x.pdf") == "http://localhost:8000/api/uploads/r2/submissions/x.pdf"
    assert svc.get_url("uploads/abc.pdf") == "http://localhost:8000/api/uploads/r2/uploads/abc.pdf"


def test_upload_calls_put_object_and_returns_url():
    svc = _service()
    url = svc.upload(b"hello", "uploads/x.txt", "text/plain")
    svc._client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="uploads/x.txt", Body=b"hello", ContentType="text/plain",
    )
    assert url == "http://localhost:8000/api/uploads/r2/uploads/x.txt"


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


def test_build_key_keeps_extension_for_dotfile_style_name():
    # Регрессия: os.path.splitext(".pdf") трактует ведущую точку как unix
    # hidden-file имя (stem=".pdf", ext="") — итоговый ключ терял расширение
    # целиком ("attachments/pdf" вместо "attachments/file.pdf").
    svc = _service()
    svc._client.head_object.side_effect = _client_error(404)
    key = svc.build_key("attachments", ".pdf")
    assert key == "attachments/file.pdf"


def test_upload_passes_cache_control_when_given():
    svc = _service()
    svc.upload(b"data", "materials/covers/x.webp", "image/webp", cache_control="public, max-age=31536000, immutable")
    svc._client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="materials/covers/x.webp", Body=b"data", ContentType="image/webp",
        CacheControl="public, max-age=31536000, immutable",
    )


def test_upload_omits_cache_control_when_not_given():
    svc = _service()
    svc.upload(b"data", "uploads/x.pdf", "application/pdf")
    kwargs = svc._client.put_object.call_args.kwargs
    assert "CacheControl" not in kwargs


def test_get_object_returns_body_etag_and_cache_control():
    svc = _service()
    svc._client.get_object.return_value = {
        "Body": io.BytesIO(b"hello world"),
        "ContentType": "image/webp",
        "ETag": '"abc123"',
        "CacheControl": "public, max-age=31536000, immutable",
        "ContentLength": 11,
    }
    obj = svc.get_object("materials/covers/x.webp")
    assert obj.body == b"hello world"
    assert obj.etag == "abc123"
    assert obj.cache_control == "public, max-age=31536000, immutable"
    assert obj.partial is False
    svc._client.get_object.assert_called_once_with(Bucket="test-bucket", Key="materials/covers/x.webp")


def test_get_object_passes_range_header_and_reports_partial():
    svc = _service()
    svc._client.get_object.return_value = {
        "Body": io.BytesIO(b"ell"),
        "ContentType": "image/webp",
        "ETag": '"abc123"',
        "ContentRange": "bytes 1-3/11",
        "ContentLength": 3,
    }
    obj = svc.get_object("materials/covers/x.webp", range_header="bytes=1-3")
    svc._client.get_object.assert_called_once_with(
        Bucket="test-bucket", Key="materials/covers/x.webp", Range="bytes=1-3",
    )
    assert obj.partial is True
    assert obj.content_range == "bytes 1-3/11"


def test_get_object_returns_none_on_404():
    svc = _service()
    svc._client.get_object.side_effect = _client_error(404)
    assert svc.get_object("materials/covers/missing.webp") is None


def test_put_if_absent_writes_when_key_free():
    svc = _service()
    assert svc.put_if_absent(b"data", "uploads/x.txt", "text/plain") is True
    kwargs = svc._client.put_object.call_args.kwargs
    assert kwargs["IfNoneMatch"] == "*"
    assert kwargs["Key"] == "uploads/x.txt"


def test_put_if_absent_returns_false_on_precondition_failed():
    svc = _service()
    svc._client.put_object.side_effect = _client_error(412)
    assert svc.put_if_absent(b"data", "uploads/x.txt", "text/plain") is False


def test_upload_unique_atomically_avoids_toctou_collision():
    # Регрессия: раньше build_key() (exists()-проверка) и upload() были
    # раздельными вызовами — два параллельных запроса с одинаковым именем
    # файла могли оба увидеть "ключ свободен" и один PUT молча перезаписывал
    # файл другого. upload_unique() пишет условно (IfNoneMatch=*) и просто
    # переходит к следующему кандидату при коллизии — второй файл никогда не
    # перезаписывает первый.
    svc = _service()
    # Первый кандидат уже занят (гонка/коллизия) -> 412, второй свободен.
    svc._client.put_object.side_effect = [_client_error(412), {}]
    url = svc.upload_unique(b"data", "submissions", "report.pdf", "application/pdf")
    assert url == "http://localhost:8000/api/uploads/r2/submissions/report_1.pdf"
    assert svc._client.put_object.call_count == 2
    first_key = svc._client.put_object.call_args_list[0].kwargs["Key"]
    second_key = svc._client.put_object.call_args_list[1].kwargs["Key"]
    assert first_key == "submissions/report.pdf"
    assert second_key == "submissions/report_1.pdf"


def test_upload_unique_falls_back_to_uuid_after_exhausting_suffixes():
    svc = _service()
    svc._client.put_object.side_effect = [_client_error(412)] * 21 + [{}]
    url = svc.upload_unique(b"data", "materials", "report.pdf", "application/pdf")
    assert svc._client.put_object.call_count == 22
    last_key = svc._client.put_object.call_args_list[-1].kwargs["Key"]
    assert last_key.startswith("materials/report_") and last_key.endswith(".pdf")
    tail = last_key[len("materials/report_"):-len(".pdf")]
    assert tail not in {str(i) for i in range(1, 21)}
    assert url.endswith(last_key)
