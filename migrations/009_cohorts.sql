-- Учебные потоки (когорты): класс = вечный шаблон, поток = конкретный учебный год.
-- Новые таблицы cohorts / cohort_students / deadlines, поле classes.rotation_mode
-- и submissions.deadline_id. Существующие данные не удаляются.
--
-- В Postgres выполнить для КАЖДОЙ схемы (university, school), как и остальные
-- миграции. Python-компаньон делает это сам (DDL + бэкфилл по обеим схемам):
--
--     python migrations/009_backfill_cohorts.py
--
-- Для новых БД таблицы создаёт Base.metadata.create_all (они есть в models.py);
-- этот файл нужен существующим базам.

-- 1) Режим ротации класса: 'manual' (по умолчанию) | 'yearly' (ежегодный rollover).
ALTER TABLE classes ADD COLUMN IF NOT EXISTS rotation_mode VARCHAR(16) NOT NULL DEFAULT 'manual';

-- 2) Потоки. status: 'active' | 'archived'.
CREATE TABLE IF NOT EXISTS cohorts (
    id            SERIAL PRIMARY KEY,
    class_id      INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    academic_year VARCHAR(16) NOT NULL,
    start_date    DATE NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_cohorts_class_id ON cohorts (class_id);
-- У одного класса не более одного активного потока (partial unique index).
CREATE UNIQUE INDEX IF NOT EXISTS ux_cohorts_one_active_per_class
    ON cohorts (class_id) WHERE status = 'active';

-- 3) Членство в потоке. class_members остаётся и продолжает наполняться
--    (двойная запись) — её читают permissions.py и легаси-скрипты.
CREATE TABLE IF NOT EXISTS cohort_students (
    cohort_id  INTEGER NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at  TIMESTAMP,
    PRIMARY KEY (cohort_id, student_id)
);

-- 4) Дедлайны заданий по потокам. is_published=false — черновик после rollover,
--    ученикам не виден, пока преподаватель не опубликует.
CREATE TABLE IF NOT EXISTS deadlines (
    id            SERIAL PRIMARY KEY,
    cohort_id     INTEGER NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    due_date      TIMESTAMP NOT NULL,
    is_published  BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT ux_deadlines_cohort_assignment UNIQUE (cohort_id, assignment_id)
);
CREATE INDEX IF NOT EXISTS ix_deadlines_cohort_id ON deadlines (cohort_id);
CREATE INDEX IF NOT EXISTS ix_deadlines_assignment_id ON deadlines (assignment_id);

-- 5) Привязка сдачи к дедлайну её потока. NULL — легаси-сдачи заданий без
--    потока; для них действует deprecated-поле assignments.deadline.
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS deadline_id INTEGER REFERENCES deadlines(id);
CREATE INDEX IF NOT EXISTS ix_submissions_deadline_id ON submissions (deadline_id);

-- 6) Бэкфилл данных (поток 2025/2026 для каждого класса, перенос участников,
--    дедлайны из assignments.deadline, deadline_id в сдачах) — Python-скриптом:
--
--        python migrations/009_backfill_cohorts.py
--
--    Логика переноса (валидность class_id, идемпотентность) непортабельна
--    в чистом SQL между SQLite/Postgres — как и в миграции 005.

-- SQLite (локальная разработка): таблицы создаст create_all при старте,
-- колонки добавит 009_backfill_cohorts.py (SQLite не умеет IF NOT EXISTS
-- для колонок, скрипт проверяет их наличие через PRAGMA/inspector).

-- ─── Downgrade (раскомментировать при откате; данные новых таблиц теряются,
--     легаси-таблицы и assignments.deadline не затронуты) ───────────────────
-- ALTER TABLE submissions DROP COLUMN IF EXISTS deadline_id;
-- DROP TABLE IF EXISTS deadlines;
-- DROP TABLE IF EXISTS cohort_students;
-- DROP TABLE IF EXISTS cohorts;
-- ALTER TABLE classes DROP COLUMN IF EXISTS rotation_mode;
