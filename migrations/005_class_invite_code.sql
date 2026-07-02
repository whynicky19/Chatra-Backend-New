-- Adds classes.invite_code (server-generated, unique 6-char join code) and
-- removes the client-side deterministic-code vulnerability (any client could
-- compute a valid join code purely from a class id).
--
-- Apply in three steps, in order:
--
-- 1) Add the column, nullable for now (existing rows don't have a code yet).
ALTER TABLE classes ADD COLUMN IF NOT EXISTS invite_code VARCHAR(6);

-- 2) Backfill existing rows so already-distributed (printed/sent) codes for
--    existing classes keep working. This uses the OLD deterministic client
--    formula, not the new random generator — run the companion Python script,
--    since the formula's floor-division loop isn't portably expressible in
--    plain SQL across SQLite/Postgres:
--
--        python migrations/005_backfill_invite_codes.py
--
--    The script also handles the (very unlikely) case where two ids collide
--    on the deterministic formula, falling back to a random code for the
--    second one so the UNIQUE constraint below can be applied cleanly.

-- 3) After the backfill script reports success, enforce the constraints:
ALTER TABLE classes ALTER COLUMN invite_code SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_classes_invite_code ON classes (invite_code);
