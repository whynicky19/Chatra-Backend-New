"""Отправка транзакционных писем через Brevo HTTP API.

Конфигурация из окружения:

    BREVO_API_KEY   — API key Brevo
    SMTP_FROM      — подтверждённый email отправителя
    SMTP_FROM_NAME — имя отправителя (по умолчанию Chatra)

Необходимые зависимости:
    httpx

Для разработки можно включить:
    OTP_DEBUG=1

Тогда dev-код дополнительно возвращается API там, где это предусмотрено
бэкендом.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def brevo_configured() -> bool:
    """Проверяет, настроен ли Brevo API."""
    return bool(os.getenv("BREVO_API_KEY", "").strip())


def otp_debug() -> bool:
    """DEV-режим для тестирования OTP."""
    return os.getenv("OTP_DEBUG", "").strip() == "1"


def send_email(
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> bool:
    """Отправляет письмо через Brevo HTTP API.

    Возвращает:
        True  — письмо успешно принято Brevo.
        False — конфигурация отсутствует или отправка не удалась.
    """

    api_key = os.getenv("BREVO_API_KEY", "").strip()

    if not api_key:
        logger.warning(
            "BREVO_API_KEY не настроен — письмо для %s не отправлено. "
            "Тема: %s",
            to,
            subject,
        )
        return False

    from_addr = os.getenv(
        "SMTP_FROM",
        "noreply@chatra.app",
    ).strip()

    from_name = os.getenv(
        "SMTP_FROM_NAME",
        "Chatra",
    ).strip()

    payload = {
        "sender": {
            "name": from_name,
            "email": from_addr,
        },
        "to": [
            {
                "email": to,
            }
        ],
        "subject": subject,
        "textContent": text_body,
    }

    if html_body:
        payload["htmlContent"] = html_body

    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                BREVO_API_URL,
                headers=headers,
                json=payload,
            )

        if 200 <= response.status_code < 300:
            logger.info(
                "Письмо успешно отправлено через Brevo на %s",
                to,
            )
            return True

        logger.error(
            "Brevo не отправил письмо на %s. "
            "HTTP %s: %s",
            to,
            response.status_code,
            response.text[:1000],
        )
        return False

    except httpx.TimeoutException:
        logger.error(
            "Таймаут при обращении к Brevo API для %s",
            to,
        )
        return False

    except httpx.RequestError as e:
        logger.error(
            "Ошибка соединения с Brevo API для %s: %s",
            to,
            e,
        )
        return False

    except Exception as e:
        logger.exception(
            "Неожиданная ошибка отправки письма на %s: %s",
            to,
            e,
        )
        return False


def send_code_email(
    to: str,
    code: str,
    purpose: str,
) -> bool:
    """Отправляет письмо с 6-значным кодом.

    purpose:
        verify — подтверждение email
        reset  — сброс пароля
    """

    if purpose == "reset":
        subject = "Сброс пароля Chatra"
        intro = (
            "Вы запросили сброс пароля. "
            "Введите этот код в приложении:"
        )
    else:
        subject = "Подтверждение email в Chatra"
        intro = (
            "Добро пожаловать в Chatra! "
            "Введите этот код для подтверждения email:"
        )

    text_body = (
        f"{intro}\n\n"
        f"    {code}\n\n"
        "Код действует 10 минут. "
        "Если вы не запрашивали это письмо — проигнорируйте его."
    )

    html_body = f"""
<div style="
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    max-width:440px;
    margin:0 auto;
    padding:24px;
    color:#1a1a1a;
">
    <h2 style="margin:0 0 8px;">Chatra</h2>

    <p style="
        color:#555;
        font-size:15px;
        line-height:1.5;
    ">
        {intro}
    </p>

    <div style="
        font-size:34px;
        font-weight:800;
        letter-spacing:8px;
        text-align:center;
        background:#f4f5f7;
        border-radius:14px;
        padding:20px 0;
        margin:18px 0;
        color:#111;
    ">
        {code}
    </div>

    <p style="
        color:#888;
        font-size:13px;
        line-height:1.5;
    ">
        Код действует 10 минут.
        Если вы не запрашивали это письмо,
        просто проигнорируйте его.
    </p>
</div>
"""

    # Для локальной разработки.
    if not brevo_configured():
        logger.warning(
            "[DEV] Brevo не настроен. Код %s для %s (%s)",
            code,
            to,
            purpose,
        )
        return False

    if otp_debug():
        logger.warning(
            "[OTP_DEBUG] Код %s для %s (%s)",
            code,
            to,
            purpose,
        )

    return send_email(
        to=to,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )