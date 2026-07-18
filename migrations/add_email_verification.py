"""Миграция: колонка users.is_verified (подтверждение email кодом).

`create_all` не добавляет колонки в существующие таблицы, поэтому is_verified
добавляем вручную. Существующие аккаунты помечаем подтверждёнными (DEFAULT true),
чтобы верификация не заблокировала вход уже зарегистрированным пользователям —
она обязательна только для НОВЫХ регистраций.

Таблица email_codes отдельной миграции не требует: это новая таблица, её создаст
Base.metadata.create_all при старте приложения.

Идемпотентно. Запуск (из каталога бэкенда):
  ./venv/bin/python migrations/add_email_verification.py
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND, ".env"))

from sqlalchemy import text
from db import engine

with engine.begin() as conn:
    conn.execute(text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT true"
    ))
print("OK: колонка users.is_verified на месте (существующие аккаунты = подтверждены)")
