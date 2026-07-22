"""Миграция: удаление таблиц чата и связанной UGC-модерации.

Продукт свернул peer-to-peer чат и переехал на ИИ-платформу (обучение),
поэтому чаты/сообщения/реакции и жалобы/блок-листы пользователей больше не
нужны — соответствующие модели и роутеры удалены из кода. Миграция дропает
таблицы. Порядок важен из-за FK: сначала зависимые (reactions, chat_members),
потом messages/chats. reports/user_blocks зависят только от users — порядок
между ними не важен.

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/drop_chat_tables.py

Скрипт ничего не выполняет автоматически при импорте — только при прямом
запуске (`python migrations/drop_chat_tables.py`).
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

from sqlalchemy import text
from db import engine, DATABASE_URL

# Дропаем в порядке, безопасном для FK: сначала таблицы, ссылающиеся на
# messages/chats, затем сами messages/chats, затем независимые reports/user_blocks.
DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS reactions CASCADE",
    "DROP TABLE IF EXISTS chat_members CASCADE",
    "DROP TABLE IF EXISTS messages CASCADE",
    "DROP TABLE IF EXISTS chats CASCADE",
    "DROP TABLE IF EXISTS reports CASCADE",
    "DROP TABLE IF EXISTS user_blocks CASCADE",
]


def main():
    print(f"DB: {DATABASE_URL}")
    with engine.begin() as conn:
        for stmt in DROP_STATEMENTS:
            print(f"-> {stmt}")
            conn.execute(text(stmt))
    print("OK: таблицы чата и UGC-модерации удалены")


if __name__ == "__main__":
    main()
