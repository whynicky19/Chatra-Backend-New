"""Регрессия: crud.assignments.update_assignment/services.file_cleanup.
delete_assignment_files должны реально удалять файлы, а не молча
no-op'иться.

1) reference_solution_url может хранить JSON-массив (несколько файлов
   эталона) — раньше при замене старый файл удалялся, передавая
   delete_upload_file() СЫРУЮ JSON-строку целиком, а не отдельные URL;
   _r2_key_from_url резал её как один путь на первом "/uploads/" и получал
   мусорный ключ, которого никогда не было в хранилище — файл не удалялся.

2) Файлы задания (в отличие от лекций) не имеют своей колонки — они
   встроены прямо в текст description. delete_assignment_files раньше их
   вообще не видел, поэтому файлы задания переживали удаление/правку
   задания как osиротевший мусор в хранилище."""
import services.file_cleanup as file_cleanup
from crud import assignments as crud_assignments
from tests.conftest import make_user


def _spy_delete(monkeypatch):
    calls = []

    def fake_delete(url):
        calls.append(url)
        return True

    monkeypatch.setattr(file_cleanup, "delete_upload_file", fake_delete)
    # crud/assignments.py делает `from services.file_cleanup import
    # delete_upload_file`, связывая своё локальное имя на момент импорта —
    # патч атрибута модуля выше его не достаёт, патчим отдельно.
    monkeypatch.setattr(crud_assignments, "delete_upload_file", fake_delete)
    return calls


def test_update_assignment_deletes_dropped_reference_files_from_json_array(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    import json
    old_urls = [
        "https://cdn.example/api/uploads/r2/assignments/a.pdf",
        "https://cdn.example/api/uploads/r2/assignments/b.pdf",
    ]
    assignment = crud_assignments.create_assignment(
        db, 1, "HW1", None, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
        reference_solution_url=json.dumps(old_urls),
    )

    new_urls = [old_urls[0], "https://cdn.example/api/uploads/r2/assignments/c.pdf"]
    crud_assignments.update_assignment(db, assignment.id, {
        "reference_solution_url": json.dumps(new_urls),
    })

    # b.pdf выпал из нового набора — должен быть удалён. a.pdf остался — не трогаем.
    # c.pdf — новый файл, нечего удалять (он не был "старым").
    assert calls == ["https://cdn.example/api/uploads/r2/assignments/b.pdf"]


def test_update_assignment_deletes_dropped_task_files_from_description(db_session, monkeypatch):
    calls = _spy_delete(monkeypatch)
    db = db_session
    teacher = make_user(db, role="teacher")
    old_description = (
        "Условие задачи.\n"
        "https://cdn.example/api/uploads/r2/attachments/card%202.jpg?exp=1&sig=aa#card%202.jpg\n"
        "https://cdn.example/api/uploads/r2/attachments/keep.docx?exp=1&sig=bb#keep.docx"
    )
    assignment = crud_assignments.create_assignment(
        db, 1, "HW1", old_description, [{"name": "ok", "weight": 100}], 100, None, teacher.id,
    )

    new_description = (
        "Условие задачи.\n"
        "https://cdn.example/api/uploads/r2/attachments/keep.docx?exp=1&sig=bb#keep.docx"
    )
    crud_assignments.update_assignment(db, assignment.id, {"description": new_description})

    assert calls == ["https://cdn.example/api/uploads/r2/attachments/card%202.jpg?exp=1&sig=aa#card%202.jpg"]


def test_delete_assignment_files_removes_files_embedded_in_description():
    task_description = (
        "Условие задачи.\n"
        "https://cdn.example/api/uploads/r2/attachments/task1.pdf?exp=1&sig=aa#task1.pdf\n"
        "https://cdn.example/api/uploads/r2/attachments/task2.docx?exp=1&sig=bb#task2.docx"
    )

    class _FakeAssignment:
        reference_solution_url = None
        description = task_description
        submissions = []

    class _FakeStorage:
        def __init__(self):
            self.deleted = []

        def delete(self, key):
            self.deleted.append(key)
            return True

    fake_storage = _FakeStorage()
    import services.file_cleanup as fc
    orig_get_storage = fc.get_storage_service
    fc.get_storage_service = lambda: fake_storage
    try:
        removed = fc.delete_assignment_files(_FakeAssignment())
    finally:
        fc.get_storage_service = orig_get_storage

    assert removed == 2
    # _r2_key_from_url режет до конца маркера "/uploads/r2/" включительно —
    # префикс "r2/" в возвращаемый ключ не входит (см. services/file_cleanup.py).
    assert set(fake_storage.deleted) == {
        "attachments/task1.pdf",
        "attachments/task2.docx",
    }
