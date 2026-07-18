"""Миграция: колонка users.token_version (отзыв токенов при logout/смене пароля).

`Base.metadata.create_all` не добавляет колонки в существующие таблицы — поэтому
колонку добавляем вручную. Идемпотентно (ADD COLUMN IF NOT EXISTS).

Запуск (из каталога бэкенда):
  ./venv/bin/python migrations/add_token_version.py
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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0"
    ))
print("OK: колонка users.token_version на месте (default 0)")
