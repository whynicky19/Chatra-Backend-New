"""Миграция: мульти-чаты (треды) главного ассистента «Chatra AI».

Таблица `ai_threads` + колонка `ai_messages.thread_id`. Тред — именованный,
закрепляемый диалог главного ассистента (class_id IS NULL). ИИ-репетиторы
классов (class_id задан) тредов не имеют — их сообщения оставляют thread_id NULL.

Бэкфилл идемпотентен: трогает только строки главного ассистента без треда
(class_id IS NULL AND thread_id IS NULL) — каждому такому пользователю заводит
один тред и привязывает к нему всю его до-миграционную глобальную историю.
Заголовок треда — первые ~40 символов самого раннего сообщения пользователя.

Вся миграция идемпотентна (IF NOT EXISTS / фильтр thread_id IS NULL), повторный
запуск безопасен.

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/add_ai_threads.py
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
        CREATE TABLE IF NOT EXISTS ai_threads (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(120) NOT NULL DEFAULT 'Новый чат',
            pinned BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ai_threads_user_sort "
        "ON ai_threads (user_id, pinned, updated_at)"
    ))
    conn.execute(text(
        "ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS thread_id INTEGER "
        "REFERENCES ai_threads(id) ON DELETE CASCADE"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ai_messages_thread_id "
        "ON ai_messages (thread_id)"
    ))

    # ── Бэкфилл: по одному треду на пользователя с осиротевшей глобальной историей ──
    user_ids = [
        row[0]
        for row in conn.execute(text(
            "SELECT DISTINCT user_id FROM ai_messages "
            "WHERE class_id IS NULL AND thread_id IS NULL"
        ))
    ]

    backfilled = 0
    for user_id in user_ids:
        first_user_msg = conn.execute(
            text(
                "SELECT content FROM ai_messages "
                "WHERE user_id = :uid AND class_id IS NULL AND thread_id IS NULL "
                "AND role = 'user' "
                "ORDER BY id ASC LIMIT 1"
            ),
            {"uid": user_id},
        ).scalar()

        if first_user_msg:
            snippet = first_user_msg.strip()
            title = snippet[:40] + ("…" if len(snippet) > 40 else "")
        else:
            title = "Чат"

        # Значения pinned/created_at/updated_at передаём явно, а не полагаемся на
        # DB-дефолты колонок: если ai_threads уже существовала (например, её
        # заранее создал SQLAlchemy Base.metadata.create_all при старте
        # приложения — тот не проставляет server-side DEFAULT, только
        # ORM-side), голый INSERT (user_id, title) падает NotNullViolation.
        new_thread_id = conn.execute(
            text(
                "INSERT INTO ai_threads (user_id, title, pinned, created_at, updated_at) "
                "VALUES (:uid, :title, FALSE, now(), now()) RETURNING id"
            ),
            {"uid": user_id, "title": title},
        ).scalar()

        conn.execute(
            text(
                "UPDATE ai_messages SET thread_id = :tid "
                "WHERE user_id = :uid AND class_id IS NULL AND thread_id IS NULL"
            ),
            {"tid": new_thread_id, "uid": user_id},
        )
        backfilled += 1

    print(f"Бэкфилл: создано тредов и привязано историй — {backfilled} пользователей")

print("OK: ai_threads готова")
