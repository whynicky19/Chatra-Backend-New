"""Отправка push-уведомлений через Firebase Cloud Messaging (HTTP v1 API).

Аутентификация — сервисным аккаунтом Firebase (google-auth). JSON ключа берётся
из окружения:
  FCM_SERVICE_ACCOUNT_FILE — путь к файлу ключа, либо
  FCM_SERVICE_ACCOUNT_JSON — само содержимое JSON инлайном.
project_id читается из ключа.

Если ключ не задан — сервис деградирует тихо: пуши не шлются, пишется
предупреждение в лог. Регистрация токенов и остальной бэкенд работают как обычно.

Публичное API:
  send_to_tokens(tokens, title, body, data) -> list[str] невалидных токенов
  send_push(db, user_ids, title, body, data) — берёт токены юзеров, шлёт, чистит битые
  send_push_bg(user_ids, title, body, data) — то же, но в фоновом потоке (не блокирует)
"""
import json
import logging
import os
import threading
from typing import Iterable, Optional

import requests

logger = logging.getLogger("fcm")

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_TIMEOUT = 10

# Кэш учётных данных и project_id между вызовами.
_credentials = None
_project_id: Optional[str] = None
_load_failed = False


def _load_service_account() -> Optional[dict]:
    raw = os.getenv("FCM_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("FCM_SERVICE_ACCOUNT_JSON: невалидный JSON")
            return None
    path = os.getenv("FCM_SERVICE_ACCOUNT_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.error("FCM_SERVICE_ACCOUNT_FILE не прочитан (%s): %s", path, e)
            return None
    return None


def _get_credentials():
    """Ленивая инициализация google-auth credentials. Возвращает (creds, project_id)
    или (None, None), если ключ не сконфигурирован/битый."""
    global _credentials, _project_id, _load_failed
    if _credentials is not None:
        return _credentials, _project_id
    if _load_failed:
        return None, None

    info = _load_service_account()
    if not info:
        _load_failed = True
        logger.warning(
            "FCM не сконфигурирован (нет FCM_SERVICE_ACCOUNT_FILE/JSON) — пуши отключены."
        )
        return None, None
    try:
        from google.oauth2 import service_account  # локальный импорт: зависимость нужна только тут

        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_FCM_SCOPE]
        )
        _credentials = creds
        _project_id = info.get("project_id")
        if not _project_id:
            logger.error("В сервисном аккаунте FCM нет project_id")
            _load_failed = True
            return None, None
        return _credentials, _project_id
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось инициализировать FCM credentials: %s", e)
        _load_failed = True
        return None, None


def _access_token() -> Optional[str]:
    creds, _ = _get_credentials()
    if creds is None:
        return None
    try:
        from google.auth.transport.requests import Request

        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось обновить токен доступа FCM: %s", e)
        return None


def send_to_tokens(
    tokens: Iterable[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> list[str]:
    """Шлёт одинаковое уведомление на набор токенов. Возвращает список токенов,
    которые FCM отверг как невалидные (их следует удалить из БД)."""
    tokens = [t for t in dict.fromkeys(tokens) if t]  # dedup, отбрасываем пустые
    if not tokens:
        return []

    access_token = _access_token()
    project_id = _project_id
    if not access_token or not project_id:
        return []

    url = _FCM_ENDPOINT.format(project_id=project_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    # data-поля в FCM должны быть строками.
    str_data = {str(k): str(v) for k, v in (data or {}).items()}

    invalid: list[str] = []
    for token in tokens:
        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": str_data,
                "android": {"priority": "high"},
                "apns": {
                    "payload": {"aps": {"sound": "default"}},
                    "headers": {"apns-priority": "10"},
                },
            }
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            logger.warning("FCM: сетевая ошибка при отправке: %s", e)
            continue
        if resp.status_code == 200:
            continue
        # 404 UNREGISTERED / 400 с INVALID_ARGUMENT по token — токен мёртв.
        if resp.status_code in (400, 404):
            err = resp.text or ""
            if "UNREGISTERED" in err or "INVALID_ARGUMENT" in err or resp.status_code == 404:
                invalid.append(token)
            logger.info("FCM отверг токен (%s): %s", resp.status_code, err[:200])
        elif resp.status_code == 401:
            logger.error("FCM 401 — проверь сервисный аккаунт/права.")
        else:
            logger.warning("FCM %s: %s", resp.status_code, (resp.text or "")[:200])
    return invalid


def send_push(
    db,
    user_ids: Iterable[int],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> int:
    """Синхронно: находит FCM-токены пользователей, шлёт уведомление и удаляет
    невалидные токены. Возвращает число токенов, на которые ушла отправка."""
    from models import DeviceToken

    user_ids = list({int(u) for u in user_ids})
    if not user_ids:
        return 0
    rows = db.query(DeviceToken).filter(DeviceToken.user_id.in_(user_ids)).all()
    tokens = [r.token for r in rows]
    if not tokens:
        return 0

    invalid = send_to_tokens(tokens, title, body, data)
    if invalid:
        try:
            db.query(DeviceToken).filter(DeviceToken.token.in_(invalid)).delete(
                synchronize_session=False
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return len(tokens)


def notify_admins(org_type: str, title: str, body: str, data: Optional[dict] = None) -> None:
    """Push всем активным админам организации (жалобы, заявки на аватары,
    лекции на модерации). Адресаты выбираются в короткой собственной сессии,
    отправка — фоново; вызывающий код не роняем никогда."""
    try:
        from sqlalchemy import text as _sql

        from db import SessionLocal

        db = SessionLocal()
        try:
            rows = db.execute(
                _sql(
                    "SELECT id FROM users "
                    "WHERE role = 'admin' AND org_type = :org AND is_active = true"
                ),
                {"org": org_type},
            ).fetchall()
            ids = [r.id for r in rows]
        finally:
            db.close()
        if ids:
            send_push_bg(ids, title, body, data)
    except Exception as e:  # noqa: BLE001
        logger.warning("notify_admins: %s", e)


def send_push_bg(
    user_ids: Iterable[int],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> None:
    """Fire-and-forget отправка в отдельном потоке с собственной сессией БД.
    Безопасно вызывать из sync- и async-обработчиков: не блокирует ответ/event loop
    и не роняет запрос, если FCM недоступен."""
    ids = list({int(u) for u in user_ids})
    if not ids:
        return

    def _worker():
        from db import SessionLocal

        db = SessionLocal()
        try:
            send_push(db, ids, title, body, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("send_push_bg: %s", e)
        finally:
            db.close()

    threading.Thread(target=_worker, daemon=True).start()
