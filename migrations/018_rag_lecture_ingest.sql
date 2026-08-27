-- Подключает существовавшую, но никогда не использовавшуюся RAG-инфраструктуру
-- (rag_documents/rag_chunks, pgvector из 002_rag_pgvector.sql) к реальному
-- конвейеру: репетитор класса раньше работал через client-side lecture_context
-- (Flutter собирал текст ВСЕХ лекций и слал его в /ai/chat целиком) — теперь
-- сервер сам инжестит лекции при создании/правке (crud/posts.py,
-- services/rag_ingest.py) и ищет релевантные чанки через pgvector
-- (services/rag_search.py) вместо того, чтобы пихать все лекции в промпт.
--
-- Run this AFTER 002_rag_pgvector.sql (нужен embedding_vec/HNSW-индекс).
-- SQLAlchemy на новых/тестовых БД создаёт эти колонки сама при
-- Base.metadata.create_all() — эта миграция нужна для БОЕВОЙ БД, где таблицы
-- rag_documents/rag_chunks уже существуют в старой (неиспользуемой) схеме.

-- 1. rag_documents: привязка к лекции (Posts) и классу, дедуп по content_hash.
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE;
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS class_id INTEGER;
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS file_url TEXT;
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_rag_documents_post_id ON rag_documents (post_id);
CREATE INDEX IF NOT EXISTS ix_rag_documents_class_id ON rag_documents (class_id);
CREATE INDEX IF NOT EXISTS ix_rag_documents_content_hash ON rag_documents (content_hash);

-- Существующие строки (если такие каким-то образом появились руками) не
-- имеют file_url — уникальный индекс ниже требует backfill, иначе все NULL
-- считались бы различными в Postgres (NULL <> NULL), что технически ОК, но
-- на всякий случай проставляем синтетическое значение по id.
UPDATE rag_documents SET file_url = 'legacy:' || id WHERE file_url IS NULL;
ALTER TABLE rag_documents ALTER COLUMN file_url SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE rag_documents ADD CONSTRAINT ux_rag_documents_post_file UNIQUE (post_id, file_url);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 2. rag_chunks: денормализованные class_id/post_id/org_type (поиск без
-- JOIN), page_number, source_type.
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS class_id INTEGER;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS post_id INTEGER;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS org_type VARCHAR NOT NULL DEFAULT 'university';
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS page_number INTEGER;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS source_type VARCHAR(16) NOT NULL DEFAULT 'text';

CREATE INDEX IF NOT EXISTS ix_rag_chunks_class_id ON rag_chunks (class_id);
CREATE INDEX IF NOT EXISTS ix_rag_chunks_post_id ON rag_chunks (post_id);

-- Составной индекс под самый частый запрос поиска: "чанки этого класса
-- (опционально — этой лекции), упорядоченные по векторной близости".
CREATE INDEX IF NOT EXISTS ix_rag_chunks_class_post ON rag_chunks (class_id, post_id);

-- 3. embedding_vec (VECTOR(1536)) из 002_rag_pgvector.sql мог быть создан
-- до появления новых колонок выше — пересоздаём HNSW-индекс на всякий
-- случай (IF NOT EXISTS — не дублирует, если он уже есть и валиден).
CREATE INDEX IF NOT EXISTS rag_chunks_embedding_hnsw
    ON rag_chunks
    USING hnsw (embedding_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Индекс для быстрой фильтрации ANN-поиска по классу/лекции ДО сортировки
-- по расстоянию (pgvector сам не умеет частичные предфильтры так же
-- эффективно, как обычный btree — но он всё равно ускоряет WHERE-часть
-- запроса services/rag_search.py::_search_pgvector).
CREATE INDEX IF NOT EXISTS ix_rag_chunks_class_org ON rag_chunks (class_id, org_type);
