"""Cloudflare R2 (S3-совместимый API) как хранилище новых файлов.

URL-схема:
  * Приватные категории (submissions, assignments, attachments, ...) — всегда
    через "{APP_BASE_URL}/api/uploads/r2/{key}", подписанный HMAC (SEC-1). Так
    было с самого начала переезда на R2 и не меняется.
  * Публичные категории (см. services/storage/base.py: PUBLIC_KEY_PREFIXES —
    сейчас только обложки классов, materials/covers/...) — если задан
    R2_PUBLIC_BASE_URL, отдают прямую публичную ссылку на R2/CDN в обход
    прокси и подписи целиком (обложки не содержат ничего секретного). Без
    R2_PUBLIC_BASE_URL они тоже пока идут через прокси — это временный
    фолбэк, а не постоянный режим: R2_PUBLIC_BASE_URL нужно включить в
    Cloudflare (R2 → бакет → Settings → Public access, или свой домен) —
    это не часть S3 API, из кода не включается.
  Раздача приватных и публичных-без-домена URL — через /api/uploads/{filename:path}
  в main.py: после проверки подписи путь с префиксом "r2/" стримится из R2
  (см. get_object), без него — читается с локального диска, как раньше.
"""
import logging
import os
import time
from dataclasses import dataclass

import botocore.exceptions
from botocore.client import Config as BotoConfig
import boto3

from services.storage.base import StorageError, StorageService, is_public_key

logger = logging.getLogger(__name__)

_RETRYABLE_EXCEPTIONS = (
    botocore.exceptions.EndpointConnectionError,
    botocore.exceptions.ConnectTimeoutError,
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ConnectionError,
)


def _is_retryable_client_error(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, botocore.exceptions.ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500
    return False


def _with_retries(fn, *, attempts: int = 3, base_delay: float = 0.5, op: str = "r2"):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == attempts or not _is_retryable_client_error(exc):
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("R2 %s: попытка %s/%s не удалась (%s), повтор через %.1fs", op, attempt, attempts, exc, delay)
            time.sleep(delay)
    logger.error("R2 %s: не удалось выполнить операцию после %s попыток: %s", op, attempts, last_exc)
    raise StorageError(f"Ошибка хранилища R2 ({op}): {last_exc}") from last_exc


class R2StorageService(StorageService):
    def __init__(self, *, bucket: str, endpoint_url: str, access_key_id: str,
                 secret_access_key: str, public_base_url: str | None, app_base_url: str):
        self._bucket = bucket
        self._public_base_url = (public_base_url or "").rstrip("/") or None
        self._app_base_url = app_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 0}),
        )

    @classmethod
    def from_env(cls) -> "R2StorageService":
        def _require(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(
                    f"Переменная окружения {name} не задана. Загрузка файлов в Cloudflare R2 "
                    f"невозможна без неё — см. .env.example (секция R2)."
                )
            return value

        account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
        endpoint_url = os.getenv("R2_ENDPOINT_URL", "").strip() or (
            f"https://{account_id}.r2.cloudflarestorage.com" if account_id else ""
        )
        if not endpoint_url:
            raise RuntimeError(
                "Не задан ни R2_ACCOUNT_ID, ни R2_ENDPOINT_URL — не могу построить endpoint Cloudflare R2."
            )

        return cls(
            bucket=_require("R2_BUCKET_NAME"),
            endpoint_url=endpoint_url,
            access_key_id=_require("R2_ACCESS_KEY_ID"),
            secret_access_key=_require("R2_SECRET_ACCESS_KEY"),
            public_base_url=os.getenv("R2_PUBLIC_BASE_URL"),
            app_base_url=os.getenv("APP_BASE_URL", "http://localhost:8000"),
        )

    def upload(self, content: bytes, key: str, content_type: str = "application/octet-stream",
               cache_control: str | None = None) -> str:
        kwargs = {"Bucket": self._bucket, "Key": key, "Body": content, "ContentType": content_type}
        if cache_control:
            kwargs["CacheControl"] = cache_control
        _with_retries(lambda: self._client.put_object(**kwargs), op=f"upload({key})")
        return self.get_url(key)

    def replace(self, content: bytes, key: str, content_type: str = "application/octet-stream",
                cache_control: str | None = None) -> str:
        return self.upload(content, key, content_type, cache_control)

    def put_if_absent(self, content: bytes, key: str, content_type: str = "application/octet-stream",
                       cache_control: str | None = None) -> bool:
        # IfNoneMatch="*" — condition write, поддерживается R2 (S3-совместимый
        # API): PUT проходит, только если объекта с таким key ещё нет.
        # Precondition Failed (412) — ключ уже занят, это ожидаемый исход
        # гонки/коллизии имён, а не ошибка хранилища.
        kwargs = {
            "Bucket": self._bucket, "Key": key, "Body": content,
            "ContentType": content_type, "IfNoneMatch": "*",
        }
        if cache_control:
            kwargs["CacheControl"] = cache_control

        def _put() -> bool:
            try:
                self._client.put_object(**kwargs)
                return True
            except botocore.exceptions.ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if status == 412:
                    return False
                raise

        return _with_retries(_put, op=f"put_if_absent({key})")

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        _with_retries(
            lambda: self._client.delete_object(Bucket=self._bucket, Key=key),
            op=f"delete({key})",
        )
        return True

    def exists(self, key: str) -> bool:
        def _head():
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return True
            except botocore.exceptions.ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if status == 404:
                    return False
                raise

        return _with_retries(_head, op=f"head({key})")

    def get_url(self, key: str) -> str:
        if self._public_base_url and is_public_key(key):
            return f"{self._public_base_url}/{key}"
        return f"{self._app_base_url}/api/uploads/r2/{key}"

    def get_object(self, key: str, range_header: str | None = None) -> "R2Object | None":
        """Читает объект (целиком или по Range) для прокси-эндпоинта
        /api/uploads/r2/<key> в main.py. None — объекта нет (404), без ретраев и
        лишнего логирования как для настоящей ошибки."""
        def _get():
            kwargs = {"Bucket": self._bucket, "Key": key}
            if range_header:
                kwargs["Range"] = range_header
            try:
                resp = self._client.get_object(**kwargs)
            except botocore.exceptions.ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if status in (404, 416):
                    return None
                raise
            return R2Object(
                body=resp["Body"].read(),
                content_type=resp.get("ContentType") or "application/octet-stream",
                etag=(resp.get("ETag") or "").strip('"'),
                cache_control=resp.get("CacheControl"),
                content_length=resp.get("ContentLength", 0),
                content_range=resp.get("ContentRange"),
                partial="ContentRange" in resp,
            )

        return _with_retries(_get, op=f"get({key})")


@dataclass(frozen=True)
class R2Object:
    body: bytes
    content_type: str
    etag: str
    cache_control: str | None
    content_length: int
    content_range: str | None
    partial: bool
