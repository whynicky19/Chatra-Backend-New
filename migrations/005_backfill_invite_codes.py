"""Backfill classes.invite_code for rows that predate the column.

Uses the legacy client-side deterministic formula (Flutter/Nuxt), so codes
already handed out for existing classes keep working:

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    n = id * 1337 + 42
    6 iterations: code += alphabet[n % 32]; n = n // 32 + id * 7

Run after the ADD COLUMN step and before the SET NOT NULL / CREATE UNIQUE
INDEX step in migrations/005_class_invite_code.sql:

    python migrations/005_backfill_invite_codes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import SessionLocal
from models import Class
from services.invite_codes import legacy_deterministic_code, random_code


def backfill():
    db = SessionLocal()
    try:
        existing_codes = {
            c.invite_code
            for c in db.query(Class).filter(Class.invite_code.isnot(None)).all()
        }
        rows = db.query(Class).filter(Class.invite_code.is_(None)).order_by(Class.id).all()

        updated = 0
        collisions = 0
        for c in rows:
            code = legacy_deterministic_code(c.id)
            attempts = 0
            while code in existing_codes:
                code = random_code()
                collisions += 1
                attempts += 1
                if attempts > 50:
                    raise RuntimeError(f"Could not backfill a unique invite_code for class id={c.id}")
            c.invite_code = code
            existing_codes.add(code)
            updated += 1

        db.commit()
        print(f"Backfilled {updated} class(es); {collisions} collision(s) resolved with a random code.")
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
