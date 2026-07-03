"""Общий rate-limiter со скользящим окном.

Ограничение текущей реализации (in-memory): состояние живёт в памяти
процесса — не переживает рестарт и не разделяется между воркерами
uvicorn/gunicorn. Интерфейс RateLimiter намеренно узкий (hit/reset),
чтобы позже подменить хранилище на Redis без правок роутеров.
"""
import time
from collections import defaultdict

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._store: dict = defaultdict(list)

    def hit(self, key) -> bool:
        """Регистрирует попытку. False — лимит исчерпан (попытка не записывается)."""
        now = time.time()
        timestamps = [t for t in self._store[key] if now - t < self.window_seconds]
        if len(timestamps) >= self.max_calls:
            self._store[key] = timestamps
            return False
        timestamps.append(now)
        self._store[key] = timestamps
        return True

    def check(self, key, detail: str = "Слишком много запросов. Попробуйте позже."):
        """hit() + HTTPException 429 при превышении."""
        if not self.hit(key):
            raise HTTPException(status_code=429, detail=detail)

    def reset(self, key) -> None:
        self._store.pop(key, None)


def client_ip(request: Request) -> str:
    """IP клиента. За реверс-прокси настройте uvicorn --proxy-headers /
    ProxyHeadersMiddleware, чтобы request.client отражал X-Forwarded-For."""
    return request.client.host if request.client else "unknown"
