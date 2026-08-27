-- Флаг «безлимитный ИИ» для пользователя.
-- Раньше сайт и приложение уже показывали тумблер и слали
-- PUT /admin/users/{id}/ai_unlimited, но на бэке колонки/эндпоинта не было —
-- теперь колонка добавлена в models.User, эндпоинт — в routers/admin.py.
-- Для новых БД колонку создаёт Base.metadata.create_all.
ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_unlimited BOOLEAN NOT NULL DEFAULT FALSE;

-- SQLite (локальная разработка) не поддерживает IF NOT EXISTS для колонок —
-- выполнить, ошибку "duplicate column name" игнорировать:
-- ALTER TABLE users ADD COLUMN ai_unlimited INTEGER NOT NULL DEFAULT 0;
