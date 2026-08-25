#!/usr/bin/env python3
"""Uptime-монитор Chatra: https://chatra.aican.cloud

Каждые CHECK_INTERVAL секунд проверяет UPTIME_URL. Если сайт не отвечает
FAILS_N раз подряд — ОДНО уведомление 🚨 в Telegram (без спама: пока сервер
лежит, повторных сообщений нет). Когда снова заработал — одно 🟢 RECOVERED.

Запускать НА ОТДЕЛЬНОЙ машине или в отдельном процессе, а не в том же
uvicorn-воркере: если упадёт весь сервер, монитор должен выжить. Подходит
systemd-таймер (см. deploy/uptime-monitor.*) или любой планировщик.

Зависимости: только httpx (уже есть в requirements.txt).
Состояние хранится в STATE_FILE, так что перезапуск монитора не порождает
ложных «упал/поднялся» сообщений.

Env:
  TELEGRAM_BOT_TOKEN   -- токен бота (@BotFather)
  TELEGRAM_CHAT_ID     -- id чата для алертов
  UPTIME_URL           -- по умолчанию https://chatra.aican.cloud/health
  CHECK_INTERVAL       -- пауза между проверками, сек (по умолч. 60)
  FAILS_N              -- сколько проверок подряд считать падением (по умолч. 3)
  UPTIME_STATE_FILE    -- файл состояния (по умолч. /tmp/chatra_uptime.json)
"""
import json
import logging
import os
import sys
import time
from datetime import datetime

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("uptime")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
URL = os.getenv("UPTIME_URL", "https://chatra.aican.cloud/health").strip()
INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
FAILS_N = int(os.getenv("FAILS_N", "3"))
STATE_FILE = os.getenv("UPTIME_STATE_FILE", "/tmp/chatra_uptime.json")
TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "10"))

if not TOKEN or not CHAT_ID:
    log.error("TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID обязательны")
    sys.exit(1)


def _now_local() -> str:
    # Время в сообщении — локальное серверное.
    return datetime.now().astimezone().strftime("%H:%M")


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"down": False, "fails": 0}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning("state write failed: %s", e)


def send_telegram(text: str) -> bool:
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=TIMEOUT,
        )
        return r.status_code == 200
    except Exception as e:
        log.error("telegram send failed: %s", e)
        return False


def check_once(client: httpx.Client) -> bool:
    """True = сайт отвечает."""
    try:
        r = client.get(URL, timeout=TIMEOUT)
        return r.status_code < 500
    except Exception as e:
        log.info("check failed: %s", e)
        return False


def main() -> None:
    state = load_state()
    log.info(
        "monitor start url=%s interval=%ss fails_n=%s down=%s fails=%s",
        URL, INTERVAL, FAILS_N, state.get("down"), state.get("fails"),
    )
    client = httpx.Client()
    while True:
        ok = check_once(client)
        state["fails"] = 0 if ok else state.get("fails", 0) + 1

        if not ok and not state["down"] and state["fails"] >= FAILS_N:
            send_telegram(
                "🚨 CHATRA ERROR\n\n"
                "Тип: Uptime\n"
                f"Сайт недоступен: {URL}\n"
                f"Проверок подряд без ответа: {state['fails']}\n\n"
                "Environment: production\n"
                f"Time: {_now_local()}"
            )
            state["down"] = True
            log.error("site DOWN after %s checks", state["fails"])

        if ok and state["down"]:
            send_telegram(
                "🟢 CHATRA RECOVERED\n\n"
                "Сайт снова доступен.\n"
                f"Time: {_now_local()}"
            )
            state["down"] = False
            log.info("site RECOVERED")

        if state["down"] and not ok:
            # Пока лежит — молчим (антиспам), но логируем.
            log.info("still down (%s)", state["fails"])

        save_state(state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
