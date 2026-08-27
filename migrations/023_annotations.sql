-- Выделения (highlights) и заметки к тексту лекций — общая сущность сайта и
-- приложения. Раньше выделения жили только в localStorage браузера: с телефона
-- их не было видно вообще, а смена браузера/устройства теряла их молча.
--
-- Позиция намеренно описана ТОЛЬКО текстом (см. models.Annotation): смещения в
-- потоке текста поверхности + якорь prefix/selected_text/suffix. Никаких
-- координат и пикселей — иначе выделение, сделанное на телефоне, не совпало бы
-- с местом на широком экране сайта.
CREATE TABLE IF NOT EXISTS annotations (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lecture_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    class_id      INTEGER NOT NULL,
    -- -1 — выделение в тексте самой лекции, иначе индекс вложения.
    file_index    INTEGER NOT NULL DEFAULT -1,
    -- Страница PDF (1..N); 0 — документ без страниц.
    page          INTEGER NOT NULL DEFAULT 0,
    selected_text TEXT    NOT NULL,
    prefix        TEXT    NOT NULL DEFAULT '',
    suffix        TEXT    NOT NULL DEFAULT '',
    start_offset  INTEGER NOT NULL DEFAULT 0,
    end_offset    INTEGER NOT NULL DEFAULT 0,
    color         VARCHAR(16) NOT NULL DEFAULT 'yellow',
    comment       TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at    TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_annotations_user_lecture ON annotations (user_id, lecture_id);
-- Инкрементальная синхронизация клиентов: «что изменилось с прошлого захода».
CREATE INDEX IF NOT EXISTS ix_annotations_user_updated ON annotations (user_id, updated_at);
