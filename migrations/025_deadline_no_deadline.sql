-- 025: флаг "без дедлайна" в deadlines.
-- Учитель может опубликовать задание без конкретной даты сдачи (тренировка,
-- открытое задание). Раньше Deadline-строка создавалась только при наличии
-- даты, поэтому такое задание было видно только через fallback на
-- assignments.deadline — и в "Заданиях и дедлайнах" (cohort-deadlines) его
-- нельзя было отдельно опубликовать. Колонка no_deadline + due_date остаётся
-- NOT NULL (placeholder для автопроверки / напоминаний, которые для
-- no_deadline=True не срабатывают).
--
-- Идемпотентно: ADD COLUMN IF NOT EXISTS поддерживается и Postgres 9.6+, и
-- SQLite 3.35+; на более старых SQLite сначала прогоняется отдельный шаг
-- (см. ниже) — но в проекте SQLite ≥ 3.35 (см. downgrade_submissions_deadline_id
-- в 009_backfill_cohorts.py: SQLite 3.35+).

ALTER TABLE deadlines ADD COLUMN IF NOT EXISTS no_deadline BOOLEAN NOT NULL DEFAULT FALSE;

-- Безопасный backfill: все существующие строки получают no_deadline=FALSE
-- (дефолт колонки) — явно для наглядности в логах миграции, поведение не
-- меняется (ранее "без дедлайна" обрабатывалось отсутствием строки).
UPDATE deadlines SET no_deadline = FALSE WHERE no_deadline IS NULL;
