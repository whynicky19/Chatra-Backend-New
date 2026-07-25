"""BE-10: очистка облачных/локальных файлов при удалении класса, задания,
варианта и аккаунта. Проверяем два свойства:
1) файлы, реально принадлежащие удаляемой сущности, чистятся;
2) удаление класса не задевает данные (в т.ч. файлы) других сущностей —
   assignments/submissions не привязаны к Class внешним ключом и переживают
   удаление класса, поэтому их файлы не должны трогаться.
"""
import json

import services.file_cleanup as file_cleanup
from crud import classes as crud_classes
from crud import assignments as crud_assignments
from crud import posts as crud_posts
from models import Class, Assignment, AssignmentVariant, Submission, Posts
from tests.conftest import make_user


def _spy_delete(monkeypatch):
    calls = []

    def fake_delete(url):
        calls.append(url)
        return True

    monkeypatch.setattr(file_cleanup, "delete_upload_file", fake_delete)
    # crud/classes.py does `from services.file_cleanup import delete_upload_file`,
    # binding its own local name at import time — patching the module attribute
    # above doesn't reach it, so it needs patching separately.
    monkeypatch.setattr(crud_classes, "delete_upload_file", fake_delete)
    return calls


def test_delete_class_removes_only_its_cover_and_leaves_other_data(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")

    target = crud_classes.create_class(db, "Target", None, teacher.id)
    target.cover_image = "https://cdn.example/uploads/r2/target-cover.jpg"
    target.cover_thumbnail = "https://cdn.example/uploads/r2/target-thumb.jpg"
    db.commit()

    other = crud_classes.create_class(db, "Other", None, teacher.id)
    other.cover_image = "https://cdn.example/uploads/r2/other-cover.jpg"
    db.commit()
    other_id = other.id

    assignment = crud_assignments.create_assignment(
        db, target.id, "HW1", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url="https://cdn.example/uploads/r2/ref.pdf",
    )
    student = make_user(db, role="student")
    sub = Submission(
        assignment_id=assignment.id, student_id=student.id,
        file_url="https://cdn.example/uploads/r2/sub.pdf",
    )
    db.add(sub)
    db.commit()

    assert crud_classes.delete_class(db, target.id) is True

    # Только обложка удалённого класса ушла в чистку.
    assert set(calls) == {
        "https://cdn.example/uploads/r2/target-cover.jpg",
        "https://cdn.example/uploads/r2/target-thumb.jpg",
    }

    # Класс пропал, остальное не тронуто.
    assert db.query(Class).filter(Class.id == target.id).first() is None
    assert db.query(Class).filter(Class.id == other_id).first() is not None
    assert db.query(Assignment).filter(Assignment.id == assignment.id).first() is not None
    assert db.query(Submission).filter(Submission.id == sub.id).first() is not None


def test_delete_assignment_removes_reference_variant_and_submission_files(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)

    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url="https://cdn.example/uploads/r2/ref.pdf",
    )
    variant = crud_classes.add_variant(
        db, assignment.id, 1, "https://cdn.example/uploads/r2/variant1.pdf",
    )
    student = make_user(db, role="student")
    sub = Submission(
        assignment_id=assignment.id, student_id=student.id,
        file_urls=json.dumps(["https://cdn.example/uploads/r2/sub1.pdf", "https://cdn.example/uploads/r2/sub2.pdf"]),
    )
    db.add(sub)
    db.commit()

    assert crud_assignments.delete_assignment(db, assignment.id) is True

    assert set(calls) == {
        "https://cdn.example/uploads/r2/ref.pdf",
        "https://cdn.example/uploads/r2/variant1.pdf",
        "https://cdn.example/uploads/r2/sub1.pdf",
        "https://cdn.example/uploads/r2/sub2.pdf",
    }
    assert db.query(AssignmentVariant).filter(AssignmentVariant.id == variant.id).first() is None
    assert db.query(Submission).filter(Submission.id == sub.id).first() is None


def test_add_variant_replacing_existing_cleans_old_file(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
    )
    crud_classes.add_variant(db, assignment.id, 1, "https://cdn.example/uploads/r2/v1-old.pdf")
    crud_classes.add_variant(db, assignment.id, 1, "https://cdn.example/uploads/r2/v1-new.pdf")

    assert calls == ["https://cdn.example/uploads/r2/v1-old.pdf"]


def test_delete_user_cleans_own_submission_created_assignment_and_class_cover(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    klass.cover_image = "https://cdn.example/uploads/r2/class-cover.jpg"
    db.commit()

    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url="https://cdn.example/uploads/r2/teacher-ref.pdf",
    )
    student = make_user(db, role="student")
    sub = Submission(
        assignment_id=assignment.id, student_id=student.id,
        file_url="https://cdn.example/uploads/r2/student-sub.pdf",
    )
    db.add(sub)
    crud_posts.create_new_post(
        db, title=f"[LECTURE][{klass.id}] L1",
        body='{"cover_image": "https://cdn.example/uploads/r2/lecture-cover.jpg"}',
        user_id=teacher.id,
    )
    db.commit()

    from services.file_cleanup import delete_user_files
    # Удаление аккаунта учителя каскадом (ORM cascade="all, delete-orphan" на
    # User.assignments_created/posts) уносит и его задания, и все сдачи
    # учеников по ним, и его лекции — все эти файлы должны быть учтены.
    delete_user_files(teacher)

    assert set(calls) == {
        "https://cdn.example/uploads/r2/teacher-ref.pdf",
        "https://cdn.example/uploads/r2/class-cover.jpg",
        "https://cdn.example/uploads/r2/student-sub.pdf",
        "https://cdn.example/uploads/r2/lecture-cover.jpg",
    }


def test_delete_user_cleans_own_submission_when_not_shared(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
    )
    student = make_user(db, role="student")
    sub = Submission(
        assignment_id=assignment.id, student_id=student.id,
        file_url="https://cdn.example/uploads/r2/only-student-sub.pdf",
    )
    db.add(sub)
    db.commit()

    from services.file_cleanup import delete_user_files
    delete_user_files(student)

    assert calls == ["https://cdn.example/uploads/r2/only-student-sub.pdf"]


def test_delete_post_removes_lecture_cover_file(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    post = crud_posts.create_new_post(
        db,
        title="[LECTURE][1] Intro",
        body='{"cover_image": "https://cdn.example/uploads/r2/lecture-cover.jpg", "text": "hi"}',
        user_id=teacher.id,
    )

    assert crud_posts.delete_post(db, post.id) is True

    assert calls == ["https://cdn.example/uploads/r2/lecture-cover.jpg"]
    assert db.query(Posts).filter(Posts.id == post.id).first() is None


def test_delete_post_ignores_data_uri_cover(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    post = crud_posts.create_new_post(
        db,
        title="[LECTURE][1] Legacy",
        body='{"cover_image": "data:image/png;base64,AAAA"}',
        user_id=teacher.id,
    )

    assert crud_posts.delete_post(db, post.id) is True
    assert calls == []
