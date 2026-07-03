-- Колонки file_url / is_read для messages.
-- Раньше добавлялись "лениво" через ALTER TABLE в GET /messages/chat/{id} —
-- теперь это разовая миграция, из обработчика запроса DDL убран.
-- Для новых БД колонки создаёт Base.metadata.create_all (они есть в models.Message).
-- В Postgres выполнить для каждой схемы (university, school), как и остальные миграции.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS file_url TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT FALSE;

-- SQLite (локальная разработка) не поддерживает IF NOT EXISTS для колонок —
-- выполнить по одной, ошибку "duplicate column name" игнорировать:
-- ALTER TABLE messages ADD COLUMN file_url TEXT;
-- ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0;
