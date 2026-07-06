"""Миграция 009: учебные потоки (когорты).

Делает всё, что описано в migrations/009_cohorts.sql, и бэкфилл данных:

  1. DDL: таблицы cohorts / cohort_students / deadlines, колонки
     classes.rotation_mode и submissions.deadline_id (идемпотентно).
  2. Каждому классу без потоков создаёт активный поток
     academic_year='2025/2026', start_date=2025-09-01.
  3. Переносит участников class_members -> cohort_students этого потока.
  4. Для каждого задания с deadline и ВАЛИДНЫМ class_id (есть в classes)
     создаёт запись в deadlines активного потока с той же датой.
     Задания с легаси class_id (ссылается на посты) пропускаются — для них
     остаётся fallback на assignments.deadline.
  5. Проставляет deadline_id в существующих сдачах.

В Postgres прогоняется по обеим схемам (university, school), в SQLite —
по единственной базе. Повторный запуск безопасен (NOT EXISTS-проверки).

    python migrations/009_backfill_cohorts.py            # upgrade
    python migrations/009_backfill_cohorts.py --downgrade  # откат DDL

Downgrade удаляет ТОЛЬКО новые таблицы/колонки; class_members,
assignments.deadline и прочие существующие данные не затрагиваются.
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from db import DATABASE_URL, engine, get_engine
from models import Class, Cohort, Deadline, Assignment, Submission, cohort_students

BACKFILL_ACADEMIC_YEAR = "2025/2026"
BACKFILL_START_DATE = date(2025, 9, 1)

NEW_TABLES = [Cohort.__table__, cohort_students, Deadline.__table__]

# (таблица, колонка, DDL-описание колонки)
NEW_COLUMNS = [
    ("classes", "rotation_mode", "VARCHAR(16) NOT NULL DEFAULT 'manual'"),
    ("submissions", "deadline_id", "INTEGER REFERENCES deadlines(id)"),
]


def _engines():
    """SQLite — одна база; Postgres — по движку на каждую схему,
    как в db.get_engine (search_path)."""
    if DATABASE_URL.startswith("sqlite"):
        return [("sqlite", engine)]
    return [(org, get_engine(org)) for org in ("university", "school")]


def _ensure_ddl(eng) -> None:
    from db import Base
    Base.metadata.create_all(bind=eng, tables=NEW_TABLES)

    insp = inspect(eng)
    with eng.connect() as conn:
        for table, column, ddl in NEW_COLUMNS:
            existing = {c["name"] for c in insp.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        conn.commit()


def _backfill(db) -> dict:
    stats = {"cohorts": 0, "students": 0, "deadlines": 0, "submissions": 0}

    for cls in db.query(Class).order_by(Class.id).all():
        cohort = (
            db.query(Cohort)
            .filter(Cohort.class_id == cls.id, Cohort.status == "active")
            .first()
        )
        if not cohort:
            # Классы, у которых потоки уже есть, но активного нет (после
            # ручной архивации) — не трогаем, это осознанное состояние.
            if db.query(Cohort).filter(Cohort.class_id == cls.id).first():
                continue
            cohort = Cohort(
                class_id=cls.id,
                academic_year=BACKFILL_ACADEMIC_YEAR,
                start_date=BACKFILL_START_DATE,
                status="active",
            )
            db.add(cohort)
            db.flush()
            stats["cohorts"] += 1

        # Участники класса -> поток. DISTINCT: в class_members нет PK,
        # дубли строк исторически возможны.
        res = db.execute(
            text("""
                INSERT INTO cohort_students (cohort_id, student_id, joined_at)
                SELECT DISTINCT :cohort_id, cm.user_id, :now
                FROM class_members cm
                WHERE cm.class_id = :class_id
                  AND NOT EXISTS (
                      SELECT 1 FROM cohort_students cs
                      WHERE cs.cohort_id = :cohort_id AND cs.student_id = cm.user_id
                  )
            """),
            {"cohort_id": cohort.id, "class_id": cls.id, "now": datetime.utcnow()},
        )
        stats["students"] += res.rowcount or 0

        # Дедлайны из deprecated-поля задания. Задания с легаси class_id сюда
        # не попадают — они не находятся по Assignment.class_id == cls.id.
        assignments = (
            db.query(Assignment)
            .filter(Assignment.class_id == cls.id, Assignment.deadline.isnot(None))
            .all()
        )
        for a in assignments:
            deadline = (
                db.query(Deadline)
                .filter(Deadline.cohort_id == cohort.id, Deadline.assignment_id == a.id)
                .first()
            )
            if not deadline:
                deadline = Deadline(
                    cohort_id=cohort.id,
                    assignment_id=a.id,
                    due_date=a.deadline,
                    is_published=True,
                )
                db.add(deadline)
                db.flush()
                stats["deadlines"] += 1

            updated = (
                db.query(Submission)
                .filter(
                    Submission.assignment_id == a.id,
                    Submission.deadline_id.is_(None),
                )
                .update({Submission.deadline_id: deadline.id}, synchronize_session=False)
            )
            stats["submissions"] += updated

    db.commit()
    return stats


def upgrade() -> None:
    for name, eng in _engines():
        _ensure_ddl(eng)
        db = sessionmaker(bind=eng)()
        try:
            stats = _backfill(db)
        finally:
            db.close()
        print(
            f"[{name}] cohorts +{stats['cohorts']}, "
            f"cohort_students +{stats['students']}, "
            f"deadlines +{stats['deadlines']}, "
            f"submissions.deadline_id +{stats['submissions']}"
        )


def downgrade() -> None:
    for name, eng in _engines():
        insp = inspect(eng)
        with eng.connect() as conn:
            if "submissions" in insp.get_table_names() and any(
                c["name"] == "deadline_id" for c in insp.get_columns("submissions")
            ):
                # SQLite поддерживает DROP COLUMN с 3.35, но не может дропнуть
                # колонку из табличного FOREIGN KEY (схема от create_all).
                # Оставить колонку безопасно: PRAGMA foreign_keys выключен,
                # а откаченный код её не читает.
                try:
                    conn.execute(text("ALTER TABLE submissions DROP COLUMN deadline_id"))
                except Exception as e:
                    print(f"[{name}] submissions.deadline_id оставлена ({e}); "
                          "это безопасно, колонка не используется старым кодом")
            for table in ("deadlines", "cohort_students", "cohorts"):
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            if "classes" in insp.get_table_names() and any(
                c["name"] == "rotation_mode" for c in insp.get_columns("classes")
            ):
                conn.execute(text("ALTER TABLE classes DROP COLUMN rotation_mode"))
            conn.commit()
        print(f"[{name}] downgraded: deadlines, cohort_students, cohorts, "
              f"submissions.deadline_id, classes.rotation_mode removed")


if __name__ == "__main__":
    if "--downgrade" in sys.argv:
        downgrade()
    else:
        upgrade()
