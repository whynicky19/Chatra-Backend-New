-- Adds classes.cover_image / teacher / period, used by the web app's class
-- cards and creation form.
--
-- All nullable: existing classes keep their current appearance until a
-- teacher changes the relevant fields.
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_image TEXT;
ALTER TABLE classes ADD COLUMN IF NOT EXISTS teacher VARCHAR(200);
ALTER TABLE classes ADD COLUMN IF NOT EXISTS period VARCHAR(100);
