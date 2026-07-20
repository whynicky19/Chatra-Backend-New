"""Отправка транзакционных писем через SMTP (Brevo и совместимые).

Конфигурация из окружения (.env):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS  — параметры SMTP-провайдера;
  SMTP_FROM        — адрес отправителя (напр. noreply@chatra.kz);
  SMTP_FROM_NAME   — имя отправителя (по умолчанию 'Chatra').

Если SMTP не сконфигурирован (нет SMTP_HOST) — письмо не отправляется, код
пишется в лог (dev-режим, чтобы флоу работал до подключения Brevo). При
OTP_DEBUG=1 код дополнительно возвращается в ответе API (только для разработки
и тестов — в проде НЕ включать)."""
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip())


def otp_debug() -> bool:
    return os.getenv("OTP_DEBUG", "").strip() == "1"


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Best-effort отправка. Возвращает True, если письмо ушло; False — если
    SMTP не настроен или отправка не удалась (код при этом уже в логе)."""
    if not smtp_configured():
        logger.warning("SMTP не настроен — письмо для %s не отправлено. Тема: %s", to, subject)
        return False

    host = os.getenv("SMTP_HOST").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    from_addr = os.getenv("SMTP_FROM", user or "noreply@chatra.app").strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Chatra").strip()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    def _deliver() -> None:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=10) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, password)
                s.send_message(msg)

    # Временные отказы (SMTP 4.x.x) стоит повторить: типичный случай — Gmail
    # отвечает «421 4.7.28 rate limited» на всплеск писем с общего домена
    # провайдера. Постоянные ошибки (5.x.x — нет такого ящика, отказ в
    # авторизации) повторять бессмысленно, выходим сразу.
    delays = (2, 8)
    for attempt in range(len(delays) + 1):
        try:
            _deliver()
            return True
        except smtplib.SMTPResponseException as e:
            transient = 400 <= e.smtp_code < 500
            if not transient or attempt == len(delays):
                logger.error(
                    "Письмо на %s не отправлено (%s, код %s): %s",
                    to,
                    "временная ошибка, попытки исчерпаны" if transient else "постоянная ошибка",
                    e.smtp_code,
                    e.smtp_error,
                )
                return False
            logger.warning(
                "Временный отказ SMTP для %s (код %s), повтор через %d с",
                to, e.smtp_code, delays[attempt],
            )
            time.sleep(delays[attempt])
        except Exception as e:
            logger.error("Ошибка отправки письма на %s: %s", to, e)
            return False
    return False


def send_code_email(to: str, code: str, purpose: str) -> bool:
    """Письмо с 6-значным кодом. purpose: 'verify' | 'reset'."""
    if purpose == "reset":
        subject = "Сброс пароля Chatra"
        intro = "Вы запросили сброс пароля. Введите код в приложении:"
    else:
        subject = "Подтверждение email в Chatra"
        intro = "Добро пожаловать в Chatra! Введите код для подтверждения email:"

    text_body = (
        f"{intro}\n\n"
        f"    {code}\n\n"
        f"Код действует 10 минут. Если вы не запрашивали это письмо — проигнорируйте его."
    )
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:440px;margin:0 auto;padding:24px;color:#1a1a1a">
  <h2 style="margin:0 0 8px">Chatra</h2>
  <p style="color:#555;font-size:15px;line-height:1.5">{intro}</p>
  <div style="font-size:34px;font-weight:800;letter-spacing:8px;text-align:center;
              background:#f4f5f7;border-radius:14px;padding:20px 0;margin:18px 0;color:#111">{code}</div>
  <p style="color:#888;font-size:13px;line-height:1.5">Код действует 10 минут. Если вы не запрашивали это письмо, просто проигнорируйте его.</p>
</div>"""
    # Всегда логируем код в dev (без SMTP), чтобы флоу можно было пройти вручную.
    if not smtp_configured():
        logger.warning("[DEV] Код %s для %s (%s)", code, to, purpose)
    return send_email(to, subject, text_body, html_body)
