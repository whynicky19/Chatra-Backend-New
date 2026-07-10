"""BE-1: add UNIQUE(assignment_id, student_id) to submissions.

`Base.metadata.create_all` only creates missing tables — it never adds a
constraint to an existing one — so pre-existing databases keep letting duplicate
submissions in until this runs. The migration first collapses any duplicates
that already slipped through the TOCTOU race (keeping the earliest submission
per (assignment, student) and its grade), then creates the unique index.

    python migrations/010_submission_unique.py

Idempotent: safe to re-run. Works on SQLite (dev) and Postgres. On Postgres with
per-org schemas set DATABASE_URL / search_path per schema and run once each.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from db import engine


def _find_duplicate_keepers(conn):
    """Return list of submission ids to delete (all but the earliest per key)."""
    rows = conn.execute(text(
        """
        SELECT id, assignment_id, student_id
        FROM submissions
        ORDER BY assignment_id, student_id, id
        """
    )).fetchall()
    seen = set()
    to_delete = []
    for sub_id, assignment_id, student_id in rows:
        key = (assignment_id, student_id)
        if key in seen:
            to_delete.append(sub_id)
        else:
            seen.add(key)
    return to_delete


def migrate():
    with engine.begin() as conn:
        to_delete = _find_duplicate_keepers(conn)
        for sub_id in to_delete:
            conn.execute(text("DELETE FROM grades WHERE submission_id = :sid"), {"sid": sub_id})
            conn.execute(text("DELETE FROM submissions WHERE id = :sid"), {"sid": sub_id})
        if to_delete:
            print(f"Removed {len(to_delete)} duplicate submission(s) before adding the constraint.")

        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_submissions_assignment_student "
            "ON submissions (assignment_id, student_id)"
        ))
    print("Unique index ux_submissions_assignment_student is in place.")


if __name__ == "__main__":
    migrate()
