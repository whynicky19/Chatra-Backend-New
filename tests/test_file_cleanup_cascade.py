"""BE-10/BE-11: очистка облачных/локальных файлов и осиротевших записей БД
при удалении класса, задания и аккаунта. Проверяем свойства:
1) файлы/записи, реально принадлежащие удаляемой сущности, чистятся;
2) удаление класса не задевает данные (в т.ч. файлы) других сущностей;
3) удаление класса НЕ трогает Assignment/Submission/Grade — это намеренный
   инвариант (Assignment.class_id не FK): сдачи и оценки — история ученика,
   переживают удаление класса. Но лекции (Posts), их файлы, RAG-индекс
   (RagDocument/RagChunk) и логи/сообщения ИИ-репетитора класса (AiUsageLog/
   AiMessage) при удалении класса чистятся — они принадлежат только классу.
"""
import json

import services.file_cleanup as file_cleanup
from crud import classes as crud_classes
from crud import assignments as crud_assignments
from crud import posts as crud_posts
from models import (
    Class, Assignment, Submission, Posts, RagDocument, RagChunk, AiUsageLog, AiMessage,
)
from tests.conftest import make_user


def _spy_delete(monkeypatch):
    calls = []

    def fake_delete(url):
        calls.append(url)
        return True

    monkeypatch.setattr(file_cleanup, "delete_upload_file", fake_delete)
    # crud/classes.py and crud/assignments.py do
    # `from services.file_cleanup import delete_upload_file`, binding their own
    # local name at import time — patching the module attribute above doesn't
    # reach those, so they need patching separately.
    monkeypatch.setattr(crud_classes, "delete_upload_file", fake_delete)
    monkeypatch.setattr(crud_assignments, "delete_upload_file", fake_delete)
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


def test_delete_class_removes_lecture_posts_and_their_files(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    target = crud_classes.create_class(db, "Target", None, teacher.id)
    other = crud_classes.create_class(db, "Other", None, teacher.id)

    lecture = crud_posts.create_new_post(
        db, title=f"[LECTURE][{target.id}] L1",
        body=json.dumps({
            "content": "text",
            "cover_image": "https://cdn.example/uploads/r2/lecture-cover.jpg",
            "files": [
                "https://cdn.example/uploads/r2/lecture-file1.pdf",
                "https://cdn.example/uploads/r2/lecture-file2.docx",
            ],
        }),
        user_id=teacher.id,
    )
    other_lecture = crud_posts.create_new_post(
        db, title=f"[LECTURE][{other.id}] L1",
        body=json.dumps({"cover_image": "https://cdn.example/uploads/r2/other-lecture-cover.jpg"}),
        user_id=teacher.id,
    )

    assert crud_classes.delete_class(db, target.id) is True

    assert set(calls) == {
        "https://cdn.example/uploads/r2/lecture-cover.jpg",
        "https://cdn.example/uploads/r2/lecture-file1.pdf",
        "https://cdn.example/uploads/r2/lecture-file2.docx",
    }
    assert db.query(Posts).filter(Posts.id == lecture.id).first() is None
    assert db.query(Posts).filter(Posts.id == other_lecture.id).first() is not None


def test_delete_class_removes_rag_documents_and_chunks(db_session, monkeypatch):
    _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    target = crud_classes.create_class(db, "Target", None, teacher.id)
    other = crud_classes.create_class(db, "Other", None, teacher.id)

    lecture = crud_posts.create_new_post(
        db, title=f"[LECTURE][{target.id}] L1", body=json.dumps({"content": "x"}), user_id=teacher.id,
    )
    doc = RagDocument(
        filename="f", mime_type="text/plain", org_type="university",
        post_id=lecture.id, class_id=target.id, file_url=f"lecture-body:{lecture.id}",
        content_hash="hash-target-body",
    )
    db.add(doc)
    db.flush()
    chunk = RagChunk(
        document_id=doc.id, chunk_index=0, text="x", token_count=1,
        embedding=json.dumps([0.1]), class_id=target.id, post_id=lecture.id, org_type="university",
    )
    db.add(chunk)
    # Class-level запись без поста — сейчас так никто не пишет, но колонка
    # это допускает, и очистка должна доставать и такие (см. models.py:325).
    orphan_doc = RagDocument(
        filename="f2", mime_type="text/plain", org_type="university",
        post_id=None, class_id=target.id, file_url="synthetic-orphan",
        content_hash="hash-target-orphan",
    )
    db.add(orphan_doc)
    other_lecture = crud_posts.create_new_post(
        db, title=f"[LECTURE][{other.id}] L1", body=json.dumps({"content": "y"}), user_id=teacher.id,
    )
    other_doc = RagDocument(
        filename="f3", mime_type="text/plain", org_type="university",
        post_id=other_lecture.id, class_id=other.id, file_url=f"lecture-body:{other_lecture.id}",
        content_hash="hash-other-body",
    )
    db.add(other_doc)
    db.commit()
    doc_id, chunk_id, orphan_doc_id, other_doc_id = doc.id, chunk.id, orphan_doc.id, other_doc.id

    assert crud_classes.delete_class(db, target.id) is True

    assert db.query(RagDocument).filter(RagDocument.id == doc_id).first() is None
    assert db.query(RagChunk).filter(RagChunk.id == chunk_id).first() is None
    assert db.query(RagDocument).filter(RagDocument.id == orphan_doc_id).first() is None
    assert db.query(RagDocument).filter(RagDocument.id == other_doc_id).first() is not None


def test_delete_class_removes_ai_usage_logs_and_messages(db_session, monkeypatch):
    _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    student = make_user(db, role="student")
    target = crud_classes.create_class(db, "Target", None, teacher.id)
    other = crud_classes.create_class(db, "Other", None, teacher.id)

    log = AiUsageLog(user_id=student.id, class_id=target.id, endpoint="ai_chat", org_type="university")
    other_log = AiUsageLog(user_id=student.id, class_id=other.id, endpoint="ai_chat", org_type="university")
    main_log = AiUsageLog(user_id=student.id, class_id=None, endpoint="ai_chat", org_type="university")
    msg = AiMessage(user_id=student.id, class_id=target.id, role="user", content="hi")
    other_msg = AiMessage(user_id=student.id, class_id=other.id, role="user", content="hi")
    main_msg = AiMessage(user_id=student.id, class_id=None, role="user", content="hi")
    db.add_all([log, other_log, main_log, msg, other_msg, main_msg])
    db.commit()
    log_id, other_log_id, main_log_id = log.id, other_log.id, main_log.id
    msg_id, other_msg_id, main_msg_id = msg.id, other_msg.id, main_msg.id

    assert crud_classes.delete_class(db, target.id) is True

    assert db.query(AiUsageLog).filter(AiUsageLog.id == log_id).first() is None
    assert db.query(AiUsageLog).filter(AiUsageLog.id == other_log_id).first() is not None
    assert db.query(AiUsageLog).filter(AiUsageLog.id == main_log_id).first() is not None
    assert db.query(AiMessage).filter(AiMessage.id == msg_id).first() is None
    assert db.query(AiMessage).filter(AiMessage.id == other_msg_id).first() is not None
    assert db.query(AiMessage).filter(AiMessage.id == main_msg_id).first() is not None


def test_delete_class_still_preserves_assignments_submissions_and_grades(db_session, monkeypatch):
    """Регрессия инварианта BE-10: Assignment.class_id намеренно не FK —
    задания/сдачи/оценки переживают удаление класса (история ученика)."""
    _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    student = make_user(db, role="student")
    target = crud_classes.create_class(db, "Target", None, teacher.id)

    assignment = crud_assignments.create_assignment(
        db, target.id, "HW1", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
    )
    sub = Submission(assignment_id=assignment.id, student_id=student.id)
    db.add(sub)
    db.commit()
    from models import Grade
    grade = Grade(submission_id=sub.id, score=90, graded_by="ai")
    db.add(grade)
    db.commit()
    assignment_id, sub_id, grade_id = assignment.id, sub.id, grade.id

    assert crud_classes.delete_class(db, target.id) is True

    assert db.query(Assignment).filter(Assignment.id == assignment_id).first() is not None
    assert db.query(Submission).filter(Submission.id == sub_id).first() is not None
    assert db.query(Grade).filter(Grade.id == grade_id).first() is not None


def test_delete_assignment_removes_reference_and_submission_files(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)

    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url="https://cdn.example/uploads/r2/ref.pdf",
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
        "https://cdn.example/uploads/r2/sub1.pdf",
        "https://cdn.example/uploads/r2/sub2.pdf",
    }
    assert db.query(Submission).filter(Submission.id == sub.id).first() is None


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


def test_delete_post_removes_lecture_attached_files(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    post = crud_posts.create_new_post(
        db,
        title="[LECTURE][1] Intro",
        body=json.dumps({
            "cover_image": "https://cdn.example/uploads/r2/lecture-cover.jpg",
            "files": [
                "https://cdn.example/uploads/r2/att1.pdf",
                "https://cdn.example/uploads/r2/att2.docx",
            ],
        }),
        user_id=teacher.id,
    )

    assert crud_posts.delete_post(db, post.id) is True

    assert set(calls) == {
        "https://cdn.example/uploads/r2/lecture-cover.jpg",
        "https://cdn.example/uploads/r2/att1.pdf",
        "https://cdn.example/uploads/r2/att2.docx",
    }


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


def test_update_class_replacing_cover_cleans_old_files(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    klass.cover_image = "https://cdn.example/uploads/r2/old-cover.jpg"
    klass.cover_thumbnail = "https://cdn.example/uploads/r2/old-thumb.jpg"
    db.commit()

    crud_classes.update_class(db, klass.id, {
        "cover_image": "https://cdn.example/uploads/r2/new-cover.jpg",
        "cover_thumbnail": "https://cdn.example/uploads/r2/new-thumb.jpg",
    })

    assert set(calls) == {
        "https://cdn.example/uploads/r2/old-cover.jpg",
        "https://cdn.example/uploads/r2/old-thumb.jpg",
    }


def test_update_class_without_cover_change_does_not_delete(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    klass.cover_image = "https://cdn.example/uploads/r2/cover.jpg"
    db.commit()

    crud_classes.update_class(db, klass.id, {"name": "Renamed"})

    assert calls == []


def test_update_assignment_replacing_reference_cleans_old_file(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    klass = crud_classes.create_class(db, "Class", None, teacher.id)
    assignment = crud_assignments.create_assignment(
        db, klass.id, "HW", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url="https://cdn.example/uploads/r2/old-ref.pdf",
    )

    crud_assignments.update_assignment(db, assignment.id, {
        "reference_solution_url": "https://cdn.example/uploads/r2/new-ref.pdf",
    })

    assert calls == ["https://cdn.example/uploads/r2/old-ref.pdf"]


def test_update_post_replacing_lecture_cover_cleans_old_file(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    post = crud_posts.create_new_post(
        db, title="[LECTURE][1] L1",
        body='{"cover_image": "https://cdn.example/uploads/r2/old-lecture-cover.jpg"}',
        user_id=teacher.id,
    )

    crud_posts.update_post(
        db, post.id, title="[LECTURE][1] L1",
        body='{"cover_image": "https://cdn.example/uploads/r2/new-lecture-cover.jpg"}',
    )

    assert calls == ["https://cdn.example/uploads/r2/old-lecture-cover.jpg"]


def test_update_post_removing_cover_cleans_old_file(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    post = crud_posts.create_new_post(
        db, title="[LECTURE][1] L1",
        body='{"cover_image": "https://cdn.example/uploads/r2/removed-cover.jpg"}',
        user_id=teacher.id,
    )

    crud_posts.update_post(db, post.id, title="[LECTURE][1] L1", body='{"text": "no cover now"}')

    assert calls == ["https://cdn.example/uploads/r2/removed-cover.jpg"]
