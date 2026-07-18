"""Миграция: email → нижний регистр + уникальность по паре (email, org_type).

Зачем:
  * Раньше email был уникален ГЛОБАЛЬНО (users_email_key), но login/register
    ключуют пользователя по паре (email, org_type). Регистрация того же email
    во втором org_type роняла IntegrityError → 500. Меняем на составной unique.
  * Email не нормализовался — 'User@x.com' и 'user@x.com' были разными
    аккаунтами. Приводим существующие адреса к нижнему регистру.

Порядок безопасный: сначала проверяем, не создаст ли нормализация дублей внутри
одного org_type; если создаёт — НИЧЕГО не меняем и печатаем конфликты, чтобы
их разрулили руками (иначе составной unique не построится).

Идемпотентно. Запуск (из каталога бэкенда):
  ./venv/bin/python migrations/fix_email_unique_per_org.py
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
    # 1. Не создаст ли lower(email) дубли внутри одного org_type?
    conflicts = conn.execute(text(
        """
        SELECT lower(trim(email)) AS e, org_type, count(*) AS n
        FROM users
        GROUP BY lower(trim(email)), org_type
        HAVING count(*) > 1
        """
    )).fetchall()
    if conflicts:
        print("СТОП: нормализация email создаст дубли — разрулите вручную:")
        for row in conflicts:
            print(f"  {row.e} / {row.org_type}: {row.n} аккаунта(ов)")
        sys.exit(1)

    # 2. Нормализуем существующие адреса.
    res = conn.execute(text(
        "UPDATE users SET email = lower(trim(email)) WHERE email <> lower(trim(email))"
    ))
    print(f"Нормализовано email: {res.rowcount}")

    # 3. Снимаем глобальный unique на email (constraint + возможный unique-индекс).
    conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key"))
    conn.execute(text("DROP INDEX IF EXISTS ix_users_email"))

    # 4. Добавляем составной unique (email, org_type), если его ещё нет.
    conn.execute(text(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ux_users_email_org'
            ) THEN
                ALTER TABLE users
                    ADD CONSTRAINT ux_users_email_org UNIQUE (email, org_type);
            END IF;
        END $$;
        """
    ))

    # 5. Возвращаем НЕуникальный индекс по email для быстрого поиска.
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))

print("OK: email нормализован, уникальность теперь по (email, org_type)")
