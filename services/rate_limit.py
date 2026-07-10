"""Общий rate-limiter со скользящим окном.

Хранилище выбирается по окружению:

  * REDIS_URL задан  → состояние в Redis (переживает рестарт, общее для всех
    воркеров uvicorn/gunicorn) — годится для антибрутфорса на проде.
  * REDIS_URL пуст   → in-memory (по процессу). Достаточно для локальной
    разработки и тестов; на проде с несколькими воркерами лимиты множились
    бы на число воркеров и сбрасывались при рестарте (см. SEC-8).

Интерфейс RateLimiter (hit/check/reset) одинаков для обоих бэкендов, поэтому
роутеры не знают, где лежит состояние.
"""
import logging
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Единый клиент Redis на процесс. None, если REDIS_URL не задан или пакет/сервер
# недоступен — тогда работаем in-memory.
_redis_client = None
_redis_ready = False


def _get_redis():
    global _redis_client, _redis_ready
    if _redis_ready:
        return _redis_client
    _redis_ready = True
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis  # redis>=4 включает синхронный клиент

        client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        client.ping()
        _redis_client = client
        logger.info("RateLimiter: используется Redis (%s)", url.split("@")[-1])
    except Exception as e:  # пакет не установлен или сервер недоступен
        logger.warning("RateLimiter: Redis недоступен (%s), fallback in-memory", e)
        _redis_client = None
    return _redis_client


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: float, namespace: str = "rl"):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.namespace = namespace
        self._store: dict = defaultdict(list)  # fallback in-memory

    # ── Redis-бэкенд (скользящее окно на sorted set) ────────────────────────
    def _redis_key(self, key) -> str:
        return f"{self.namespace}:{self.max_calls}:{int(self.window_seconds)}:{key}"

    def _hit_redis(self, client, key) -> bool:
        now = time.time()
        rkey = self._redis_key(key)
        cutoff = now - self.window_seconds
        try:
            pipe = client.pipeline()
            pipe.zremrangebyscore(rkey, 0, cutoff)
            pipe.zcard(rkey)
            _, count = pipe.execute()
            if count >= self.max_calls:
                return False
            member = f"{now}:{os.urandom(4).hex()}"
            pipe = client.pipeline()
            pipe.zadd(rkey, {member: now})
            pipe.expire(rkey, int(self.window_seconds) + 1)
            pipe.execute()
            return True
        except Exception as e:
            # Не роняем запрос из-за сбоя Redis — деградируем к in-memory.
            logger.warning("RateLimiter: сбой Redis, fallback in-memory (%s)", e)
            return self._hit_memory(key)

    # ── In-memory-бэкенд ────────────────────────────────────────────────────
    def _hit_memory(self, key) -> bool:
        now = time.time()
        timestamps = [t for t in self._store[key] if now - t < self.window_seconds]
        if len(timestamps) >= self.max_calls:
            self._store[key] = timestamps
            return False
        timestamps.append(now)
        self._store[key] = timestamps
        return True

    def hit(self, key) -> bool:
        """Регистрирует попытку. False — лимит исчерпан (попытка не записывается)."""
        client = _get_redis()
        if client is not None:
            return self._hit_redis(client, key)
        return self._hit_memory(key)

    def check(self, key, detail: str = "Слишком много запросов. Попробуйте позже."):
        """hit() + HTTPException 429 при превышении."""
        if not self.hit(key):
            raise HTTPException(status_code=429, detail=detail)

    def reset(self, key) -> None:
        client = _get_redis()
        if client is not None:
            try:
                client.delete(self._redis_key(key))
            except Exception as e:
                logger.warning("RateLimiter: reset Redis failed (%s)", e)
        self._store.pop(key, None)


def client_ip(request: Request) -> str:
    """IP клиента. За реверс-прокси настройте uvicorn --proxy-headers /
    ProxyHeadersMiddleware, чтобы request.client отражал X-Forwarded-For."""
    return request.client.host if request.client else "unknown"
