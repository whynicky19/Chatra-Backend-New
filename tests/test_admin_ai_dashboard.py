"""Дашборд расхода токенов: /api/admin/ai-usage/dashboard.

Проверяем ровно то, ради чего он появился: расход должен раскладываться по
видам запросов (чат / названия чатов / обложки / проверка работ), по классам,
по дням и по людям — и все эти разрезы обязаны сходиться с общим итогом.
"""
from datetime import timedelta

import pytest

from models import AiUsageLog, Class
from tests.conftest import auth_headers, make_user
from utils.time import utcnow

URL = "/api/admin/ai-usage/dashboard"

# Инвайт-код уникален — нумеруем классы, созданные в этом файле.
_counter = {"n": 0}


@pytest.fixture()
def clean_usage(db_session):
    """Тестовая БД одна на весь прогон, а дашборд суммирует все строки расхода —
    иначе записи соседних тестов ломают ожидаемые суммы."""
    db_session.query(AiUsageLog).delete()
    db_session.commit()
    return db_session


@pytest.fixture()
def admin(db_session):
    return make_user(db_session, role="admin")


def _log(db, *, user, endpoint, total, class_id=None, prompt=None, completion=None,
         days_ago=0):
    prompt = total // 2 if prompt is None else prompt
    completion = total - prompt if completion is None else completion
    row = AiUsageLog(
        user_id=user.id, class_id=class_id, endpoint=endpoint,
        org_type=user.org_type, prompt_tokens=prompt,
        completion_tokens=completion, total_tokens=total,
        created_at=utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.commit()
    return row


def _class(db, teacher, name="Алгебра"):
    _counter["n"] += 1
    cls = Class(name=name, created_by=teacher.id, org_type=teacher.org_type,
                invite_code=f"TSTDSH{_counter['n']:04d}")
    db.add(cls)
    db.commit()
    db.refresh(cls)
    return cls


def test_dashboard_splits_tokens_by_kind(client, db_session, admin, clean_usage):
    """Главный вопрос админа — «на что ушли токены»: чат, названия чатов и
    обложки не должны сливаться в одно число."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=1000)
    _log(db_session, user=teacher, endpoint="chat_vision", total=500)
    _log(db_session, user=teacher, endpoint="ai_title", total=40)
    _log(db_session, user=teacher, endpoint="cover_image", total=300)
    _log(db_session, user=teacher, endpoint="ai-grade", total=2000)

    data = client.get(URL, headers=auth_headers(admin)).json()

    assert data["totals"]["total_tokens"] == 3840
    assert data["totals"]["request_count"] == 5
    kinds = {e["endpoint"]: e["total_tokens"] for e in data["by_endpoint"]}
    assert kinds == {"chat": 1000, "chat_vision": 500, "ai_title": 40,
                     "cover_image": 300, "ai-grade": 2000}
    labels = {e["endpoint"]: e["label"] for e in data["by_endpoint"]}
    assert labels["cover_image"] == "Обложка предмета"
    assert labels["ai_title"] == "Название чата"
    # Разрезы обязаны сходиться с итогом, иначе дашборду нельзя верить.
    assert sum(kinds.values()) == data["totals"]["total_tokens"]


def test_dashboard_groups_related_endpoints(client, db_session, admin, clean_usage):
    """chat и chat_vision — одна функция продукта: «сколько стоит чат» должно
    читаться одним числом, не теряя детализации по endpoint."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=700)
    _log(db_session, user=teacher, endpoint="chat_vision", total=300)
    _log(db_session, user=teacher, endpoint="ai-grade", total=100)
    _log(db_session, user=teacher, endpoint="ai-grade-auto", total=50)

    groups = {g["group"]: g for g in client.get(URL, headers=auth_headers(admin)).json()["by_group"]}

    assert groups["chat"]["total_tokens"] == 1000
    assert set(groups["chat"]["endpoints"]) == {"chat", "chat_vision"}
    assert groups["grade"]["total_tokens"] == 150


def test_dashboard_shows_unknown_endpoint_as_is(client, db_session, admin, clean_usage):
    """Новая функция ещё без названия не должна молча выпадать из отчёта —
    иначе сумма разрезов перестанет сходиться с итогом."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="brand_new_thing", total=123)

    data = client.get(URL, headers=auth_headers(admin)).json()

    row = next(e for e in data["by_endpoint"] if e["endpoint"] == "brand_new_thing")
    assert row["label"] == "brand_new_thing"
    assert row["group"] == "other"
    assert data["totals"]["total_tokens"] == 123


def test_dashboard_daily_series_is_zero_filled(client, db_session, admin, clean_usage):
    """График по дням: дни без запросов приходят нулями, иначе паузы
    схлопываются и динамика выглядит ровной."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=100, days_ago=0)
    _log(db_session, user=teacher, endpoint="chat", total=200, days_ago=2)

    data = client.get(URL, params={"days": 5}, headers=auth_headers(admin)).json()

    by_day = data["by_day"]
    assert len(by_day) == 5
    assert [d["date"] for d in by_day] == sorted(d["date"] for d in by_day)
    assert by_day[-1]["total_tokens"] == 100
    assert by_day[-3]["total_tokens"] == 200
    assert by_day[-2]["total_tokens"] == 0
    assert by_day[-3]["kinds"] == {"chat": 200}


def test_dashboard_period_window_cuts_old_rows(client, db_session, admin, clean_usage):
    """Окно отчёта не должно затягивать старый расход: иначе «за неделю»
    показывало бы историю за всё время."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=100, days_ago=1)
    _log(db_session, user=teacher, endpoint="chat", total=900, days_ago=40)

    data = client.get(URL, params={"days": 7}, headers=auth_headers(admin)).json()

    assert data["totals"]["total_tokens"] == 100
    # А общий итог за всё время остаётся полным — по нему сверяют счёт.
    assert data["totals_all_time"]["total_tokens"] == 1000


def test_dashboard_ranks_users_and_classes(client, db_session, admin, clean_usage):
    """Кто и в каком предмете тратит: с разбивкой по видам, иначе непонятно,
    ушло это в переписку или в обложки."""
    heavy = make_user(db_session, role="student")
    heavy.full_name = "Петров Пётр"
    light = make_user(db_session, role="student")
    db_session.commit()
    cls = _class(db_session, admin, name="Физика")

    _log(db_session, user=heavy, endpoint="chat", total=5000, class_id=cls.id)
    _log(db_session, user=heavy, endpoint="cover_image", total=300, class_id=cls.id)
    _log(db_session, user=light, endpoint="chat", total=100)

    data = client.get(URL, headers=auth_headers(admin)).json()

    top = data["top_users"]
    assert top[0]["user_id"] == heavy.id
    assert top[0]["name"] == "Петров Пётр"
    assert top[0]["total_tokens"] == 5300
    assert top[0]["kinds"] == {"chat": 5000, "cover_image": 300}

    by_class = {c["class_id"]: c for c in data["by_class"]}
    assert by_class[cls.id]["class_name"] == "Физика"
    assert by_class[cls.id]["total_tokens"] == 5300
    assert by_class[cls.id]["kinds"] == {"chat": 5000, "cover_image": 300}
    # Расход вне предмета (общий чат) — отдельная строка с class_id = None.
    assert by_class[None]["total_tokens"] == 100


def test_dashboard_keeps_usage_of_deleted_class(client, db_session, admin, clean_usage):
    """Класс удалён, расход остался: строку не прячем, иначе разрезы перестанут
    сходиться с итогом."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=250, class_id=999999)

    data = client.get(URL, headers=auth_headers(admin)).json()

    row = next(c for c in data["by_class"] if c["class_id"] == 999999)
    assert row["class_name"] is None
    assert row["total_tokens"] == 250


def test_dashboard_is_scoped_to_org(client, db_session, admin, clean_usage):
    """Чужая организация не должна попадать в отчёт."""
    stranger = make_user(db_session, role="teacher", org_type="school")
    _log(db_session, user=stranger, endpoint="chat", total=777)
    own = make_user(db_session, role="teacher")
    _log(db_session, user=own, endpoint="chat", total=10)

    data = client.get(URL, headers=auth_headers(admin)).json()

    assert data["totals"]["total_tokens"] == 10
    assert data["totals_all_time"]["total_tokens"] == 10


def test_dashboard_reports_daily_budget(client, db_session, admin, clean_usage, monkeypatch):
    """Расход за сегодня читается на фоне лимита, по которому бэкенд реально
    отказывает в запросе."""
    monkeypatch.setenv("AI_DAILY_TOKEN_BUDGET", "12345")
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=400, days_ago=0)
    _log(db_session, user=teacher, endpoint="chat", total=999, days_ago=3)

    limits = client.get(URL, headers=auth_headers(admin)).json()["limits"]

    assert limits["daily_token_budget"] == 12345
    assert limits["tokens_used_today"] == 400


def test_dashboard_is_admin_only(client, db_session):
    teacher = make_user(db_session, role="teacher")
    student = make_user(db_session, role="student")
    assert client.get(URL, headers=auth_headers(teacher)).status_code == 403
    assert client.get(URL, headers=auth_headers(student)).status_code == 403


def test_usage_log_filters_by_kind_and_user(client, db_session, admin, clean_usage):
    """Клик по виду расхода в дашборде должен открывать ровно те строки, из
    которых он сложился."""
    a = make_user(db_session, role="teacher")
    b = make_user(db_session, role="teacher")
    _log(db_session, user=a, endpoint="cover_image", total=300)
    _log(db_session, user=a, endpoint="chat", total=100)
    _log(db_session, user=b, endpoint="cover_image", total=200)

    covers = client.get("/api/admin/ai-usage", params={"endpoint": "cover_image"},
                        headers=auth_headers(admin)).json()
    assert covers["total"] == 2
    assert {i["total_tokens"] for i in covers["items"]} == {300, 200}

    mine = client.get("/api/admin/ai-usage",
                      params={"endpoint": "cover_image", "user_id": a.id},
                      headers=auth_headers(admin)).json()
    assert mine["total"] == 1
    assert mine["items"][0]["total_tokens"] == 300


def test_usage_log_filters_by_group_of_kinds(client, db_session, admin, clean_usage):
    """Чат — это chat + chat_vision: клик по строке «Чат с ИИ» обязан открыть
    журнал целиком, а не одну его половину."""
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=100)
    _log(db_session, user=teacher, endpoint="chat_vision", total=200)
    _log(db_session, user=teacher, endpoint="cover_image", total=300)

    data = client.get("/api/admin/ai-usage", params={"endpoint": "chat,chat_vision"},
                      headers=auth_headers(admin)).json()

    assert data["total"] == 2
    assert sum(i["total_tokens"] for i in data["items"]) == 300


def test_usage_log_can_isolate_usage_outside_classes(client, db_session, admin, clean_usage):
    """Строка «Общий чат» тоже должна раскрываться в журнал: class_id=0 — это
    расход без предмета, а не «все классы»."""
    teacher = make_user(db_session, role="teacher")
    cls = _class(db_session, admin, name="История")
    _log(db_session, user=teacher, endpoint="chat", total=100, class_id=cls.id)
    _log(db_session, user=teacher, endpoint="chat", total=700)

    general = client.get("/api/admin/ai-usage", params={"class_id": 0},
                         headers=auth_headers(admin)).json()
    assert general["total"] == 1
    assert general["items"][0]["total_tokens"] == 700

    # Без параметра — по-прежнему всё.
    assert client.get("/api/admin/ai-usage", headers=auth_headers(admin)).json()["total"] == 2


def test_usage_log_filters_by_period(client, db_session, admin, clean_usage):
    teacher = make_user(db_session, role="teacher")
    _log(db_session, user=teacher, endpoint="chat", total=100, days_ago=1)
    _log(db_session, user=teacher, endpoint="chat", total=900, days_ago=40)

    recent = client.get("/api/admin/ai-usage", params={"days": 7},
                        headers=auth_headers(admin)).json()

    assert recent["total"] == 1
    assert recent["items"][0]["total_tokens"] == 100
