-- Adds classes.cover_thumbnail — a small (<=480px) WebP version of the class
-- cover, generated alongside the compressed main cover (see
-- services/image_processing.py, services/image_storage.py). Used by class
-- list/card views instead of the full-size cover_image.
--
-- NULL for classes whose cover was set before this migration is valid: the
-- client falls back to the full-size cover for those classes.
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_thumbnail TEXT;
