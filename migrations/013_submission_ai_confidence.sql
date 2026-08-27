-- AI confidence gate for handwritten (photo) submissions: adds columns to
-- record the recognition/quality confidence (0-100) and the reasons list
-- (JSON array of strings) whenever a submission is graded through the
-- vision path in services/ai_grader.py. Both nullable: only set for image
-- submissions, never for plain text/DOCX/PDF ones.
--
-- Run once per Postgres schema (university, school), same as the other
-- migrations here. Use an inline DATABASE_URL — db.py does not load .env.
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS ai_confidence INTEGER;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS ai_review_reasons TEXT;

-- SQLite (local dev) doesn't support IF NOT EXISTS for columns — run these
-- one at a time and ignore "duplicate column name" errors:
-- ALTER TABLE submissions ADD COLUMN ai_confidence INTEGER;
-- ALTER TABLE submissions ADD COLUMN ai_review_reasons TEXT;
