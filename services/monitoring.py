"""Мониторинг ошибок: Sentry.

Инициализация вызывается из main.py сразу после load_dotenv() и ДО создания
app, чтобы Sentry перехватывал исключения в том числе на этапе старта.
Без SENTRY_DSN всё тихо выключено — локальная разработка не затрагивается.

Приватность (SEC): в события не должны попадать access/refresh токены,
пароли, содержимое личных сообщений. Sentry по умолчанию НЕ пишет тела
запросов; дополнительно вырезаем чувствительные заголовки/куки и данные
форм авторизации в before_send.
"""
import logging
import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)

# Ключи, которые никогда не должны оказаться в событии.
_REDACTED = "[Filtered]"
_SENSITIVE_KEYS = {
    "password", "password_confirm", "new_password", "old_password",
    "access_token", "refresh_token", "token", "secret", "secret_key",
    "authorization", "cookie", "set-cookie", "api_key", "smtp_pass",
}

# Эндпоинты, где даже сам факт события не должен нести тело запроса
# (личные сообщения, чаты, ИИ-переписка) — обрезаем request.data.
_BODYLESS_PREFIXES = ("/api/posts", "/api/ai")


def _scrub(obj):
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if str(k).lower() in _SENSITIVE_KEYS else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return type(obj)(_scrub(v) for v in obj)  # type: ignore[return-value]
    return obj


def before_send(event, hint):
    """Чистит заголовки, куки и чувствительные поля до отправки."""
    try:
        req = event.get("request")
        if req:
            headers = req.get("headers")
            if isinstance(headers, dict):
                req["headers"] = {
                    k: (_REDACTED if str(k).lower() in _SENSITIVE_KEYS else v)
                    for k, v in headers.items()
                }
            cookies = req.get("cookies")
            if cookies:
                req["cookies"] = _REDACTED

            path = str(req.get("url") or "")
            data = req.get("data")
            if data and any(p in path for p in _BODYLESS_PREFIXES):
                req["data"] = _REDACTED
            elif data:
                req["data"] = _scrub(data)

        user = event.get("user")
        if isinstance(user, dict):
            # Оставляем только безличный id для сопоставления репортов.
            event["user"] = {"id": user.get("id") or user.get("user_id")}
    except Exception:
        pass
    return event


def init_monitoring() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN не задан — Sentry отключён")
        return False

    environment = os.getenv("SENTRY_ENVIRONMENT", "development")
    release = os.getenv("SENTRY_RELEASE") or None
    sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05"))

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        traces_sample_rate=sample_rate or None,
        before_send=before_send,
        max_breadcrumbs=50,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.ERROR, event_level=logging.CRITICAL),
        ],
    )
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("service", "chatra-backend")
    logger.info("Sentry инициализирован (env=%s)", environment)
    return True
