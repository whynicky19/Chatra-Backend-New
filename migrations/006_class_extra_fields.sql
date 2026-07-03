-- Adds classes.cover_image / teacher / period, used by the web app's class
-- cards and creation form but previously only stored inside Posts.body JSON
-- (see migrations/006_backfill_posts_to_classes.py for the data migration
-- off that legacy Posts-based pseudo-class system).
--
-- All nullable, no backfill needed here: classes rows don't exist yet at
-- this point, they're created by 006_backfill_posts_to_classes.py right
-- after this SQL is applied.
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_image TEXT;
ALTER TABLE classes ADD COLUMN IF NOT EXISTS teacher VARCHAR(200);
ALTER TABLE classes ADD COLUMN IF NOT EXISTS period VARCHAR(100);
