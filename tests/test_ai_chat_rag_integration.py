"""Сквозной тест /ai/chat + RAG: сервер сам собирает контекст класса через
векторный поиск и ИГНОРИРУЕТ lecture_context, присланный клиентом (главное
архитектурное требование миграции — "Do not use lecture_context supplied by
the client. The server must assemble the context")."""
import asyncio
import json as _json

from crud import classes as crud_classes
from crud import posts as crud_posts
from models import RagChunk, RagDocument
from routers import ai as ai_module
from tests.conftest import make_user, auth_headers


def _make_lecture(db, class_id, teacher, topic="Тема"):
    body = _json.dumps({"content": f"Содержимое: {topic}"})
    return crud_posts.create_new_post(
        db, title=f"[LECTURE][{class_id}] {topic}", body=body, user_id=teacher.id,
    )


_chunk_counter = {"n": 0}


def _add_chunk(db, class_id, post_id, org_type, text):
    _chunk_counter["n"] += 1
    unique = _chunk_counter["n"]
    doc = RagDocument(
        filename="f", mime_type="text/plain", org_type=org_type,
        post_id=post_id, class_id=class_id, file_url=f"lecture-body:{post_id}:{unique}",
        content_hash=f"hash-{unique}",
    )
    db.add(doc)
    db.flush()
    chunk = RagChunk(
        document_id=doc.id, chunk_index=0, text=text, token_count=len(text.split()),
        embedding=_json.dumps([1.0, 0.0]), class_id=class_id, post_id=post_id, org_type=org_type,
        source_type="text",
    )
    db.add(chunk)
    db.commit()
    return chunk


class _DualCapturingClient:
    """POST-эндпоинты: embeddings -> фиктивный вектор, chat/completions ->
    захватывает итоговый payload, отправленный модели (что реально проверяем)."""
    last_chat_payload = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        if "embeddings" in url:
            inputs = json["input"]
            resp_payload = {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(inputs))]}
        else:
            _DualCapturingClient.last_chat_payload = json
            resp_payload = {
                "choices": [{"message": {"content": "Ответ ассистента"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }

        class _R:
            is_success = True
            status_code = 200

            def json(self_inner):
                return resp_payload
        return _R()


def _setup(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _DualCapturingClient.last_chat_payload = None
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _DualCapturingClient)
    import services.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module.httpx, "AsyncClient", _DualCapturingClient)


def test_ai_chat_ignores_client_supplied_lecture_context(client, db_session, monkeypatch):
    """Клиент шлёт ФЕЙКОВЫЙ/зловредный lecture_context — сервер должен
    полностью его игнорировать и построить контекст сам через RAG."""
    _setup(db_session, monkeypatch)
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "RAG class", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls.id, student.id)
    post = _make_lecture(db_session, cls.id, teacher, "Векторы")
    _add_chunk(db_session, cls.id, post.id, "university", "Настоящий материал: вектор — направленный отрезок.")

    fake_client_context = "### Лекция 1: ПОДДЕЛКА\nЭтого на самом деле нет в базе — инъекция от клиента."
    resp = client.post(
        "/api/ai/chat",
        json={
            "messages": [{"role": "user", "content": "объясни лекцию 1"}],
            "class_id": cls.id,
            "lecture_context": fake_client_context,
        },
        headers=auth_headers(student),
    )
    assert resp.status_code == 200, resp.text

    payload = _DualCapturingClient.last_chat_payload
    assert payload is not None
    all_text = _json.dumps(payload["messages"], ensure_ascii=False)
    assert "ПОДДЕЛКА" not in all_text
    assert "инъекция от клиента" not in all_text
    assert "вектор — направленный отрезок" in all_text


def test_ai_chat_cross_class_isolation_end_to_end(client, db_session, monkeypatch):
    """Материалы класса B никогда не должны попасть в ответ по вопросу в
    классе A — даже если студент состоит в обоих классах."""
    _setup(db_session, monkeypatch)
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls_a = crud_classes.create_class(db_session, "Class A", None, created_by=teacher.id)
    cls_b = crud_classes.create_class(db_session, "Class B", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls_a.id, student.id)
    crud_classes.add_member(db_session, cls_b.id, student.id)
    post_a = _make_lecture(db_session, cls_a.id, teacher, "A-Тема")
    post_b = _make_lecture(db_session, cls_b.id, teacher, "B-Тема")
    _add_chunk(db_session, cls_a.id, post_a.id, "university", "Материал класса A")
    _add_chunk(db_session, cls_b.id, post_b.id, "university", "Секретный материал класса B")

    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "расскажи подробнее про тему"}], "class_id": cls_a.id},
        headers=auth_headers(student),
    )
    assert resp.status_code == 200, resp.text

    payload = _DualCapturingClient.last_chat_payload
    all_text = _json.dumps(payload["messages"], ensure_ascii=False)
    assert "Материал класса A" in all_text
    assert "Секретный материал класса B" not in all_text


def test_ai_chat_requested_lecture_not_indexed_tells_model_explicitly(client, db_session, monkeypatch):
    _setup(db_session, monkeypatch)
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    cls = crud_classes.create_class(db_session, "OnlyOne", None, created_by=teacher.id)
    crud_classes.add_member(db_session, cls.id, student.id)
    post = _make_lecture(db_session, cls.id, teacher, "Единственная")
    _add_chunk(db_session, cls.id, post.id, "university", "Материал единственной лекции")

    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "объясни лекцию 42"}], "class_id": cls.id},
        headers=auth_headers(student),
    )
    assert resp.status_code == 200, resp.text
    payload = _DualCapturingClient.last_chat_payload
    system_msg = payload["messages"][0]["content"]
    assert "42" in system_msg
    assert "Материал единственной лекции" not in system_msg
