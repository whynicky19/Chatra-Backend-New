"""Миграция: индекс submissions.student_id.

Выборки по студенту (/assignments/student/my-submissions, my-rating, join
рейтинга) фильтруют submissions по student_id в одиночку. Составной unique
(assignment_id, student_id) ведёт по assignment_id и такие запросы не покрывает —
без отдельного индекса шёл seq scan по растущей таблице сдач, что тормозило
главный экран студента. Миграция идемпотентна (CREATE INDEX IF NOT EXISTS).

⚠️ Запускать с ЯВНЫМ DATABASE_URL продовой базы (db.py по умолчанию — sqlite):
  DATABASE_URL="postgresql://.../test_jwt" ./venv/bin/python migrations/add_submission_student_index.py
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
        "CREATE INDEX IF NOT EXISTS ix_submissions_student_id "
        "ON submissions (student_id)"
    ))

print("OK: индекс ix_submissions_student_id готов")
