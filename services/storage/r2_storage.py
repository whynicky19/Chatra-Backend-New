"""Cloudflare R2 (S3-совместимый API) как хранилище новых файлов.

URL-схема (см. план миграции с локального uploads/):
  * Без R2_PUBLIC_BASE_URL (по умолчанию, текущий режим): get_url() возвращает
    "{APP_BASE_URL}/uploads/r2/{key}" — тот же вид, что и у локальных файлов
    "{APP_BASE_URL}/uploads/{name}". Это значит, что существующий
    UploadUrlSignerMiddleware/sign_uploads_in_text (services/file_urls.py)
    автоматически проставляет HMAC-подпись и на эти ссылки без единой правки в
    самой подписи. Раздача — через /uploads/{filename:path} в main.py: после
    проверки подписи путь с префиксом "r2/" стримится из R2, без него — читается
    с локального диска, как раньше.
  * С R2_PUBLIC_BASE_URL (заготовка под будущий CDN, например
    cdn.chatra.aican.cloud): get_url() отдаёт прямую публичную ссылку в обход
    прокси и подписи. Сейчас не используется (переменная не задана).
"""
import logging
import os
import time

import botocore.exceptions
from botocore.client import Config as BotoConfig
import boto3

from services.storage.base import StorageError, StorageService

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

    def upload(self, content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        _with_retries(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=key, Body=content, ContentType=content_type,
            ),
            op=f"upload({key})",
        )
        return self.get_url(key)

    def replace(self, content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
        return self.upload(content, key, content_type)

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
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        return f"{self._app_base_url}/uploads/r2/{key}"

    def get_object_bytes(self, key: str) -> bytes:
        """Читает объект целиком. Используется прокси-эндпоином /uploads/r2/<key>."""
        def _get() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        return _with_retries(_get, op=f"get({key})")
