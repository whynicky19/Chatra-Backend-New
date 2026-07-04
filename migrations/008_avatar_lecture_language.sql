-- Язык озвучки/конспекта аватар-лекции. Обучение идёт строго на английском,
-- поэтому дефолт 'en'. Лекции, сгенерированные до этой миграции, — русские.
ALTER TABLE avatar_lectures ADD COLUMN IF NOT EXISTS language VARCHAR(8) NOT NULL DEFAULT 'en';
ALTER TABLE avatar_lectures ALTER COLUMN language SET DEFAULT 'en';
