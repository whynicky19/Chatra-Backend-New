"""Миграция: таблица reports (жалобы на UGC, App Store Guideline 1.2).

Идемпотентна (CREATE TABLE / INDEX IF NOT EXISTS). Пара к
migrations/add_user_blocks.py — вместе они дают требуемый сторами набор:
жалоба + блокировка + очередь модерации.

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/add_reports.py
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
        CREATE TABLE IF NOT EXISTS reports (
            id SERIAL PRIMARY KEY,
            reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            org_type VARCHAR NOT NULL DEFAULT 'university',
            target_type VARCHAR(32) NOT NULL,
            target_id INTEGER NOT NULL,
            reason VARCHAR(32) NOT NULL,
            comment TEXT,
            resolved BOOLEAN NOT NULL DEFAULT false,
            resolution VARCHAR(32),
            resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT ux_reports_reporter_target
                UNIQUE (reporter_id, target_type, target_id)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_reports_reporter_id ON reports (reporter_id)"
    ))
    # Очередь модерации: открытые жалобы, старые сверху (реакция ≤ 24 ч).
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_reports_open ON reports (resolved, created_at)"
    ))

print("OK: reports готова")
