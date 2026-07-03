"""Backfill real Class rows for the legacy web "pseudo-class" system.

The web frontend used to represent a class as a Posts row whose `body` is a
JSON blob with `type == "class"` (name/description/teacher/period/cover_image/
group/created_by), instead of using the real Class model. Meanwhile
assignments.class_id, ai_usage_logs.class_id and avatar_lectures.class_id
were already being populated with these Post ids, so they're "orphaned"
against the (until now empty) classes table.

This script creates one real Class row per such Post, reusing the SAME id as
the source Post, so those already-existing class_id references become valid
without touching assignments/ai_usage_logs/avatar_lectures at all.

Run once, after applying migrations/006_class_extra_fields.sql:

    DATABASE_URL=... python migrations/006_backfill_posts_to_classes.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from db import SessionLocal
from models import Class, Posts, User
from services.invite_codes import legacy_deterministic_code, random_code


def backfill():
    db = SessionLocal()
    try:
        posts = db.query(Posts).order_by(Posts.id).all()

        existing_codes = {
            c.invite_code for c in db.query(Class).filter(Class.invite_code.isnot(None)).all()
        }
        inserted = 0
        skipped_not_class = 0
        skipped_conflict = 0

        for post in posts:
            try:
                body = json.loads(post.body)
            except (TypeError, ValueError):
                continue
            if not isinstance(body, dict) or body.get("type") != "class":
                skipped_not_class += 1
                continue

            existing = db.query(Class).filter(Class.id == post.id).first()
            if existing:
                print(f"SKIP post id={post.id}: a Class with this id already exists")
                skipped_conflict += 1
                continue

            created_by = body.get("created_by") or post.user_id
            creator = db.query(User).filter(User.id == created_by).first()
            org_type = creator.org_type if creator else "university"

            # Reuse the id-derived code the web frontend has always displayed/
            # shared for this "class" (pages/index.vue:codeFor), so codes
            # teachers already handed out keep working after this backfill.
            code = legacy_deterministic_code(post.id)
            while code in existing_codes:
                code = random_code()
            existing_codes.add(code)

            obj = Class(
                id=post.id,
                name=post.title,
                description=body.get("description") or None,
                created_by=created_by,
                created_at=post.created_at,
                is_active=True,
                group=body.get("group") or None,
                org_type=org_type,
                cover_image=body.get("cover_image") or None,
                teacher=body.get("teacher") or None,
                period=body.get("period") or None,
                invite_code=code,
            )
            db.add(obj)
            db.flush()
            inserted += 1

        db.execute(text(
            "SELECT setval('classes_id_seq', COALESCE((SELECT MAX(id) FROM classes), 1))"
        ))
        db.commit()

        print(
            f"Backfilled {inserted} class(es); "
            f"{skipped_not_class} post(s) were not classes; "
            f"{skipped_conflict} id conflict(s) skipped."
        )
    finally:
        db.close()


if __name__ == "__main__":
    backfill()
