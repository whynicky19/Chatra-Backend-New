"""Миграция: таблицы device_tokens и push_log (push-уведомления через FCM).

`Base.metadata.create_all` создаёт новые таблицы при старте, но на проде
запускаем миграцию явно, чтобы не зависеть от порядка деплоя. Идемпотентно
(CREATE TABLE IF NOT EXISTS).

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/add_push_tables.py
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from sqlalchemy import text
from db import engine, DATABASE_URL

print(f"DB: {DATABASE_URL}")

with engine.begin() as conn:
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            platform VARCHAR(16),
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            last_seen TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT ux_device_tokens_token UNIQUE (token)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_device_tokens_user_id ON device_tokens (user_id)"
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS push_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            dedup_key VARCHAR(64) NOT NULL,
            sent_at TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT ux_push_log_user_key UNIQUE (user_id, dedup_key)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_push_log_user_id ON push_log (user_id)"
    ))

print("OK: таблицы device_tokens и push_log на месте")
