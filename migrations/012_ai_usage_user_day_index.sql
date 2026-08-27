-- Дневной лимит сообщений ИИ (services/ai_quota.py) считает строки
-- ai_usage_logs по (user_id, endpoint, created_at) на каждый запрос к /ai/chat.
-- Без индекса это seq scan по растущей таблице логов.
CREATE INDEX IF NOT EXISTS ix_ai_usage_logs_user_created
    ON ai_usage_logs (user_id, created_at);

-- SQLite (локальная разработка) синтаксис поддерживает как есть.
