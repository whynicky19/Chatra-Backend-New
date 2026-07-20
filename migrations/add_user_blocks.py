"""Миграция: таблица user_blocks (серверный блок-лист, UGC Guideline 1.2).

До этого блокировки жили только в SharedPreferences на устройстве. Миграция
идемпотентна (CREATE TABLE IF NOT EXISTS).

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/add_user_blocks.py
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
        CREATE TABLE IF NOT EXISTS user_blocks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blocked_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT ux_user_blocks_pair UNIQUE (user_id, blocked_user_id)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_user_blocks_user_id ON user_blocks (user_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_user_blocks_blocked_user_id "
        "ON user_blocks (blocked_user_id)"
    ))

print("OK: user_blocks готова")
