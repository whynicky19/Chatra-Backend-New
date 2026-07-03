"""Защита от SSRF при скачивании файлов по пользовательским URL.

Разрешены только URL, указывающие на собственное хранилище приложения:
  * APP_BASE_URL (локальные /uploads)
  * Yandex Object Storage (бакет из YC_BUCKET_NAME)

Дополнительно хост резолвится и блокируются приватные/loopback/link-local
диапазоны — кроме случая, когда сам APP_BASE_URL указывает на localhost
(локальная разработка).
"""
import ipaddress
import os
import socket
from urllib.parse import urlparse


def _own_bases() -> list[tuple[str, str, int]]:
    """Список (scheme, host, port) доверенных баз."""
    bases = []
    app_base = os.getenv("APP_BASE_URL", "http://localhost:8000")
    parsed = urlparse(app_base)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        bases.append((parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)))

    bucket = os.getenv("YC_BUCKET_NAME", "").strip()
    if bucket and bucket != "your-bucket-name":
        bases.append(("https", f"{bucket}.storage.yandexcloud.net", 443))
        bases.append(("https", "storage.yandexcloud.net", 443))
    return bases


def _is_private_address(host: str) -> bool:
    """Резолвит хост и возвращает True, если хоть один адрес приватный."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # не резолвится — не ходим
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def is_safe_fetch_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        # .port может кинуть ValueError на кривых URL вида host:8000.evil.com
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False

    for base_scheme, base_host, base_port in _own_bases():
        if parsed.scheme == base_scheme and host == base_host and port == base_port:
            # Свой APP_BASE_URL может быть localhost'ом при локальной
            # разработке — это доверенная база, приватность не проверяем.
            if host in ("localhost", "127.0.0.1", "::1"):
                return True
            return not _is_private_address(host)

    return False
