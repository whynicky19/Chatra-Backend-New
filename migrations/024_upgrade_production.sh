#!/usr/bin/env sh
# Обновление уже существующей production-БД до схемы, необходимой ТЕКУЩЕМУ
# коду Chatra. Скрипт не удаляет и не «возвращает» архивные данные; его можно
# запускать повторно: все DDL-операции и Python-миграции идемпотентны.
#
# Запуск из корня backend-репозитория:
#   DATABASE_URL='postgresql://...' sh migrations/024_upgrade_production.sh
#
# ВАЖНО: перед запуском всё равно сделайте бэкап базы.

set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo 'ERROR: set DATABASE_URL to the production PostgreSQL connection string.' >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo 'ERROR: psql is required to apply SQL migrations.' >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-./venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=python3
fi

sql() {
  echo "==> $1"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$1"
}

py() {
  echo "==> $*"
  "$PYTHON_BIN" "$@"
}

# create_all добавляет только отсутствующие таблицы (без ALTER/DROP), поэтому
# гарантирует наличие новых таблиц перед миграциями их колонок и индексов.
echo '==> create missing tables from the current ORM models'
"$PYTHON_BIN" -c 'from db import engine; from models import Base; Base.metadata.create_all(bind=engine)'

# Базовые изменения существующих таблиц.
sql migrations/002_rag_pgvector.sql
sql migrations/003_class_org_type.sql
sql migrations/004_ai_usage_org_type.sql

# 005 состоит из трёх шагов: нельзя применять её SQL-файл целиком до
# бэкофилла, так как SET NOT NULL упадёт на старых классах.
echo '==> migrations/005_class_invite_code.sql (add column)'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'ALTER TABLE classes ADD COLUMN IF NOT EXISTS invite_code VARCHAR(6);'
py migrations/005_backfill_invite_codes.py
echo '==> migrations/005_class_invite_code.sql (constraints)'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'ALTER TABLE classes ALTER COLUMN invite_code SET NOT NULL;'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'CREATE UNIQUE INDEX IF NOT EXISTS ix_classes_invite_code ON classes (invite_code);'

sql migrations/006_class_extra_fields.sql

# Старый direct-messages чат в некоторых базах уже был удалён раньше, а
# текущий продукт эту таблицу не использует. ALTER выполняем только там,
# где она действительно существует.
if psql "$DATABASE_URL" -tAc "SELECT to_regclass('public.messages') IS NOT NULL" | grep -q t; then
  sql migrations/007_messages_file_url_is_read.sql
else
  echo 'SKIP migrations/007_messages_file_url_is_read.sql: legacy messages table is absent.'
fi

# 009 самостоятельно создаёт таблицы и добавляет активные потоки уже
# существующим классам — без этого текущая логика потоков и дедлайнов неполна.
py migrations/009_backfill_cohorts.py

sql migrations/011_user_ai_unlimited.sql
sql migrations/012_ai_usage_user_day_index.sql
sql migrations/013_submission_ai_confidence.sql
sql migrations/014_classes_cover_thumbnail.sql

sql migrations/017_posts_lecture_position.sql
sql migrations/018_rag_lecture_ingest.sql
py migrations/019_backfill_rag_lecture_ingest.py

sql migrations/021_class_cover_appearance.sql
sql migrations/022_users_created_at.sql
sql migrations/023_annotations.sql

# Нумерации у следующих исторических миграций нет, но они требуются текущим
# кодом: верификация, отзыв токенов, история AI-чатов, push и модерация.
py migrations/add_email_verification.py
py migrations/add_token_version.py
py migrations/fix_email_unique_per_org.py
py migrations/add_ai_threads.py
py migrations/add_push_tables.py
py migrations/add_user_blocks.py
py migrations/add_reports.py
py migrations/add_submission_student_index.py

# 025: флаг «без дедлайна» в deadlines — чтобы учитель мог опубликовать
# задание без конкретной даты сдачи.
sql migrations/025_deadline_no_deadline.sql

echo 'SUCCESS: all schema changes required by the current code are complete.'
