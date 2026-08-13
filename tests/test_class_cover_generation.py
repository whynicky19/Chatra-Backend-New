"""Генерируемые обложки классов: палитра/иконки, промпт, фолбэк, эндпоинт.

Ключевая гарантия, которую здесь проверяем со всех сторон: класс никогда не
остаётся без обложки. Ни отсутствие ключа OpenAI, ни ошибка/таймаут API, ни
битые байты в ответе не должны приводить к пустому cover_image — вместо этого
собирается локальный фолбэк в том же визуальном языке.
"""
import base64
import io
import math

import httpx
import pytest
from PIL import Image

from services import cover_art, cover_generator
from routers import classes as classes_router
from tests.conftest import auth_headers, make_user


# ── Фейковое хранилище ──────────────────────────────────────────────────────
class FakeStorage:
    """Принимает загрузки в память и отдаёт предсказуемые URL. Настоящий R2 в
    тестах не настроен, а нам нужно видеть именно те байты, что уехали бы в него."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def build_key(self, category, filename):
        return f"{category.strip('/')}/{filename}"

    def upload(self, content, key, content_type="application/octet-stream", cache_control=None):
        self.objects[key] = content
        return f"https://cdn.test/{key}"

    def exists(self, key):
        return key in self.objects


@pytest.fixture()
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr("services.image_storage.get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def teacher(db_session):
    return make_user(db_session, role="teacher")


def _make_class(client, teacher, **body):
    payload = {"name": "Матанализ", **body}
    resp = client.post("/api/classes/", json=payload, headers=auth_headers(teacher))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _png_bytes(size=(1536, 1024), color=(30, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _openai_image_response(png: bytes, usage=None):
    body = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def _patch_openai(monkeypatch, handler):
    """Подменяет httpx.AsyncClient.post внутри cover_generator, чтобы ни один
    тест не мог случайно уйти в настоящий OpenAI (и в настоящий счёт)."""
    async def fake_post(self, url, **kwargs):
        return handler(url, kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


# ── Палитра и иконки ────────────────────────────────────────────────────────
def test_catalog_lists_every_colour_and_icon():
    cat = cover_art.catalog()
    assert {c["id"] for c in cat["colors"]} == set(cover_art.PALETTE)
    assert {i["id"] for i in cat["icons"]} == set(cover_art.ICONS)
    assert cat["default_color"] in cover_art.PALETTE
    assert cat["default_icon"] in cover_art.ICONS
    for c in cat["colors"]:
        for field in ("hex", "base"):
            assert c[field].startswith("#") and len(c[field]) == 7


def _relative_luminance(hex_color: str) -> float:
    def channel(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in cover_art._rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_background_colours_are_rich_mid_tones():
    """Цвет — основа всей композиции, поэтому он должен быть выразительным.

    Диапазон закреплён тестом, потому что края уже дважды приходилось
    откатывать: почти чёрные значения давали «ночной» вид, а очень светлая
    пастель — блёклый дашбордный фон.
    """
    for slug, v in cover_art.PALETTE.items():
        r, g, b = cover_art._rgb(v["base"])
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        assert luma > 95, f"{slug}: заливка {v['base']} слишком тёмная"
        assert luma < 200, f"{slug}: заливка {v['base']} выцвела в пастель"
        # И это должен быть насыщенный ЦВЕТ, а не серый.
        assert max(r, g, b) - min(r, g, b) > 70, f"{slug}: заливка обесцвечена"


def test_icon_is_white_over_the_artwork():
    """Иконка лежит поверх произвольной точки насыщенной композиции, поэтому
    клиенты рисуют её белой с тенью (ICON_ON_ARTWORK).

    Тест сторожит именно это решение: вариант «в тон» здесь уже был и на
    нынешних цветах даёт контраст около 1.5-2.0 — иконка просто пропадает.
    """
    assert cover_art.ICON_ON_ARTWORK == "white-with-shadow"
    for slug, v in cover_art.PALETTE.items():
        white = _contrast("#FFFFFF", v["base"])
        ink = _contrast(v["ink"], v["base"])
        assert white > ink, f"{slug}: белая иконка проигрывает цветной — пересмотри тон"
        # Голого контраста мало на светлых заливках (оранжевый ~2.6), поэтому
        # у глифа на обоих клиентах есть мягкая тень. Ниже 2.5 не опускаемся.
        assert white >= 2.5, f"{slug}: белая иконка на {v['base']} — контраст {white:.2f}"


def test_every_icon_has_a_decorative_motif():
    # Мотив без описания дал бы модели пустую подсказку, и обложка предмета
    # перестала бы отличаться от любой другой.
    for slug, meta in cover_art.ICONS.items():
        assert meta["motif"].strip(), f"нет мотива для {slug}"
        assert meta["subject"].strip(), f"нет предмета для {slug}"


def test_normalize_falls_back_instead_of_raising():
    assert cover_art.normalize_color("PURPLE") == "purple"
    assert cover_art.normalize_color("chartreuse") == cover_art.DEFAULT_COLOR
    assert cover_art.normalize_color(None) == cover_art.DEFAULT_COLOR
    assert cover_art.normalize_icon("  Atom ") == "atom"
    assert cover_art.normalize_icon("banana") == cover_art.DEFAULT_ICON


def test_cover_options_endpoint(client, teacher):
    resp = client.get("/api/classes/cover/options", headers=auth_headers(teacher))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["colors"]) == len(cover_art.PALETTE)
    assert len(data["icons"]) == len(cover_art.ICONS)
    assert isinstance(data["ai_available"], bool)


# ── Промпт ──────────────────────────────────────────────────────────────────
def test_prompt_is_one_style_across_subjects():
    """Все обложки должны выглядеть одной коллекцией — значит общий каркас
    стиля обязан присутствовать в каждом промпте без исключений."""
    prompts = [
        cover_art.build_prompt(color, icon, seed=1).lower()
        for color in cover_art.PALETTE
        for icon in cover_art.ICONS
    ]
    for p in prompts:
        for required in (
            # что нельзя рисовать
            "do not place any text inside the artwork",
            "do not draw the subject icon itself",
            "no letters", "no symbols", "no logos", "no ui elements",
            "no buttons", "no cards", "no dashboards", "no photographs",
            "no people", "no realistic objects", "no photorealism",
            "no 3d rendering", "not an infographic", "not a diagram",
            "no repetitive dot grids", "no rows of rectangles", "saas dashboard",
            # сама дизайн-система
            "premium illustrated course banner", "one cohesive abstract composition",
            "flowing curves", "overlap naturally", "rich and sophisticated",
            "avoid pure black", "avoid overly pale",
            "maintain the same overall visual language",
            # запреты на старую «дашбордную» раскладку
            "do not arrange the elements into a grid", "do not create columns",
            "do not create repetitive patterns", "do not force symmetry",
            "do not divide the artwork into left and right sections",
        ):
            assert required in p, f"в промпте пропало «{required}»"
        # Центр обязан оставаться пустым в КАЖДОМ промпте: туда клиент кладёт
        # предметную иконку.
        # Пустая зона под иконку больше НЕ резервируется — она и делала
        # картинку похожей на дашборд.
        assert "left third" not in p and "right third" not in p
        assert "clean and empty" not in p


def test_prompt_carries_chosen_colour_and_subject_motif():
    p = cover_art.build_prompt("purple", "sigma", seed=1)
    assert cover_art.PALETTE["purple"]["prompt"] in p
    assert cover_art.ICONS["sigma"]["motif"] in p
    # Цвет и мотив соседнего варианта в промпт не просачиваются.
    assert cover_art.PALETTE["green"]["prompt"] not in p
    assert cover_art.ICONS["atom"]["motif"] not in p


def test_prompt_never_contains_the_subject_name():
    """Название предмета рисует UI, а не модель: любая надпись внутри картинки
    — это кривой AI-текст, который потом никак не исправить."""
    for icon in cover_art.ICONS:
        p = cover_art.build_prompt("blue", icon, seed=3)
        assert "Матанализ" not in p
        assert "class name" not in p.lower()


def test_regenerate_varies_composition_but_not_style():
    a = cover_art.build_prompt("teal", "atom", seed=1)
    b = cover_art.build_prompt("teal", "atom", seed=999)
    base = cover_art._BASE_STYLE.format(
        color=cover_art.PALETTE["teal"]["prompt"],
        motif=cover_art.ICONS["atom"]["motif"],
    )
    assert base in a and base in b       # стиль и цвет те же
    assert a != b                        # раскладка другая


def test_prompt_is_deterministic_for_a_seed():
    assert cover_art.build_prompt("red", "flask", seed=7) == cover_art.build_prompt("red", "flask", seed=7)


# ── Локальный рендер ────────────────────────────────────────────────────────
@pytest.mark.parametrize("icon", list(cover_art.ICONS))
def test_fallback_cover_renders_for_every_icon(icon):
    img = cover_art.render_fallback_cover("blue", icon, seed=5)
    assert img.size == (cover_art.COVER_WIDTH, cover_art.COVER_HEIGHT)
    assert img.mode == "RGB"


def test_fallback_uses_the_chosen_colour():
    """Выбранный цвет должен быть основным акцентом, а не декоративной деталью."""
    def dominant_hue(color):
        img = cover_art.render_fallback_cover(color, "book", seed=2).convert("HSV")
        small = img.resize((32, 32))
        hues = [small.getpixel((x, y))[0] for x in range(32) for y in range(32)]
        return sorted(hues)[len(hues) // 2]

    # Синий и оранжевый лежат на противоположных концах круга оттенков —
    # если бы цвет не доезжал до картинки, они бы совпали.
    assert abs(dominant_hue("blue") - dominant_hue("orange")) > 40


def test_fallback_is_landscape():
    """Обложку показывают широкой полосой (карточка ~2.1:1, шапка класса
    ~3.3:1). У квадрата в кадр попадала только средняя полоса — та самая,
    что оставлена чистой под иконку, отсюда и «фон пустой»."""
    img = cover_art.render_fallback_cover("blue", seed=1)
    assert img.width > img.height
    assert 1.4 < img.width / img.height < 1.7


def test_fallback_fills_the_whole_frame(seed=3):
    """Композиция должна работать на всей площади: обложку кропают под очень
    разные пропорции, и любая зарезервированная пустота (как «чистая
    центральная полоса», которая тут была) при широком кропе превращается в
    пустой фон.
    """
    for color in ("blue", "orange", "teal"):
        img = cover_art.render_fallback_cover(color, seed=seed).convert("L")
        w, h = img.size

        def spread(x0, x1):
            px = [img.getpixel((x, y))
                  for y in range(0, h, 8) for x in range(x0, x1, 8)]
            return max(px) - min(px)

        third = w // 3
        for name, x0, x1 in (("левая", 0, third),
                             ("центральная", third, 2 * third),
                             ("правая", 2 * third, w)):
            assert spread(x0, x1) > 10, f"{color}: {name} треть пустая"


def test_fallback_centre_is_not_blown_out_under_the_white_icon():
    """Иконка белая с тенью, поэтому фолбэк не должен уходить в белизну в
    центре — иначе глиф растворится даже с тенью."""
    for color in cover_art.PALETTE:
        img = cover_art.render_fallback_cover(color, seed=4)
        cx, cy = img.width // 2, img.height // 2
        half = round(img.width * 0.10)
        centre = img.crop((cx - half, cy - half, cx + half, cy + half)).convert("L")
        pixels = list(centre.getdata())
        mean = sum(pixels) / len(pixels)
        assert mean < 225, f"{color}: центр обложки выбелен до {mean:.0f}"


def test_different_colours_produce_different_backgrounds():
    a = cover_art.render_fallback_cover("teal", seed=4).tobytes()
    b = cover_art.render_fallback_cover("orange", seed=4).tobytes()
    assert a != b


def test_background_does_not_depend_on_the_icon():
    """Фон рисуется только по цвету: предмет различается декоративным мотивом
    в промпте и иконкой, которую накладывает UI, а не самим фолбэком."""
    a = cover_art.render_fallback_cover("teal", "sigma", seed=4).tobytes()
    b = cover_art.render_fallback_cover("teal", "dna", seed=4).tobytes()
    assert a == b


# ── Создание класса ─────────────────────────────────────────────────────────
def test_create_class_stores_appearance_and_a_ready_cover(client, teacher, storage):
    data = _make_class(client, teacher, cover_color="purple", cover_icon="sigma")
    assert data["cover_color"] == "purple"
    assert data["cover_icon"] == "sigma"
    assert data["cover_source"] == cover_generator.SOURCE_FALLBACK
    # Обложка готова сразу, ещё до всякой генерации.
    assert data["cover_image"] and data["cover_thumbnail"]
    assert len(storage.objects) == 2


def test_create_class_defaults_appearance_when_client_sends_none(client, teacher, storage):
    data = _make_class(client, teacher)
    assert data["cover_color"] == cover_art.DEFAULT_COLOR
    assert data["cover_icon"] == cover_art.DEFAULT_ICON
    assert data["cover_image"]


def test_create_class_does_not_call_openai(client, teacher, storage, monkeypatch):
    """Создание должно быть мгновенным и бесплатным: платная генерация —
    только отдельным явным вызовом."""
    def explode(url, kwargs):
        raise AssertionError("создание класса не должно ходить в OpenAI")

    _patch_openai(monkeypatch, explode)
    _make_class(client, teacher, cover_color="green", cover_icon="dna")


def test_create_class_rejects_unknown_colour(client, teacher, storage):
    resp = client.post("/api/classes/", json={"name": "X", "cover_color": "chartreuse"},
                       headers=auth_headers(teacher))
    assert resp.status_code == 422


def test_create_class_survives_storage_outage(client, teacher, monkeypatch):
    """Недоступное хранилище не должно стоить пользователю созданного класса."""
    monkeypatch.setattr("services.image_storage._process_and_upload", lambda raw: None)
    data = _make_class(client, teacher, cover_color="red", cover_icon="atom")
    assert data["id"]
    assert data["cover_image"] is None
    assert data["cover_color"] == "red"


# ── Генерация ───────────────────────────────────────────────────────────────
def test_generate_cover_calls_openai_and_saves_result(client, teacher, storage, monkeypatch):
    seen = {}

    def handler(url, kwargs):
        seen["url"] = url
        seen["payload"] = kwargs["json"]
        seen["auth"] = kwargs["headers"]["Authorization"]
        return _openai_image_response(_png_bytes(), usage={
            "input_tokens": 30, "output_tokens": 272, "total_tokens": 302,
        })

    _patch_openai(monkeypatch, handler)
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    before = cls["cover_image"]

    resp = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(teacher))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert seen["url"] == cover_generator.OPENAI_IMAGES_URL
    assert seen["payload"]["model"] == cover_generator.DEFAULT_MODEL
    assert seen["payload"]["quality"] == cover_generator.DEFAULT_QUALITY
    assert seen["auth"] == "Bearer test-key"
    # Цвет и иконка сохраняются, картинка — новая.
    assert data["cover_source"] == cover_generator.SOURCE_AI
    assert data["cover_color"] == "blue" and data["cover_icon"] == "atom"
    assert data["cover_image"] and data["cover_image"] != before


def test_stored_cover_is_the_bare_background(client, teacher, storage, monkeypatch):
    """Иконка в картинку НЕ впекается: её рисует UI поверх (см. докстринг
    services/cover_art.py). Если фон от модели ровный, ровным он и должен
    доехать до хранилища — появление там символа означало бы, что иконка снова
    попала в изображение и клиентский оверлей ляжет на неё вторым слоем."""
    flat = _png_bytes(color=(30, 90, 170))
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(flat))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="sigma")
    storage.objects.clear()

    client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                headers=auth_headers(teacher))

    stored = [v for k, v in storage.objects.items() if "thumbnail" not in k][0]
    with Image.open(io.BytesIO(stored)) as img:
        lo, hi = img.convert("L").getextrema()
        assert hi - lo < 20, "в сохранённой обложке что-то нарисовано поверх фона"


def test_generation_updates_appearance_when_client_picks_new_values(client, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")

    resp = client.post(f"/api/classes/{cls['id']}/cover/generate",
                       json={"color": "pink", "icon": "note"},
                       headers=auth_headers(teacher))
    assert resp.json()["cover_color"] == "pink"
    assert resp.json()["cover_icon"] == "note"


def test_regenerate_keeps_colour_and_icon(client, teacher, storage, monkeypatch):
    prompts = []

    def handler(url, kwargs):
        prompts.append(kwargs["json"]["prompt"])
        return _openai_image_response(_png_bytes())

    _patch_openai(monkeypatch, handler)
    cls = _make_class(client, teacher, cover_color="indigo", cover_icon="globe")

    first = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                        headers=auth_headers(teacher)).json()
    second = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                         headers=auth_headers(teacher)).json()

    assert first["cover_color"] == second["cover_color"] == "indigo"
    assert first["cover_icon"] == second["cover_icon"] == "globe"
    assert first["cover_image"] != second["cover_image"]
    # Один стиль, разные раскладки — иначе Regenerate давал бы другую эстетику.
    assert cover_art.PALETTE["indigo"]["prompt"] in prompts[0]
    assert cover_art.PALETTE["indigo"]["prompt"] in prompts[1]


@pytest.mark.parametrize("failure", [
    pytest.param(lambda url, kw: httpx.Response(500, json={"error": {"message": "boom"}}), id="api-error"),
    pytest.param(lambda url, kw: httpx.Response(429, json={"error": {"message": "quota"}}), id="quota-exhausted"),
    pytest.param(lambda url, kw: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")), id="timeout"),
    pytest.param(lambda url, kw: httpx.Response(200, json={"data": [{"b64_json": "!!not-base64!!"}]}), id="garbage-payload"),
    pytest.param(lambda url, kw: httpx.Response(200, json={"data": []}), id="empty-payload"),
])
def test_generation_failures_fall_back_to_a_real_cover(client, teacher, storage, monkeypatch, failure):
    _patch_openai(monkeypatch, failure)
    cls = _make_class(client, teacher, cover_color="orange", cover_icon="flask")

    resp = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(teacher))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cover_source"] == cover_generator.SOURCE_FALLBACK
    assert data["cover_image"], "при сбое генерации класс всё равно обязан получить обложку"
    assert data["cover_color"] == "orange" and data["cover_icon"] == "flask"


def test_generation_without_api_key_falls_back(client, teacher, storage, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cls = _make_class(client, teacher, cover_color="teal", cover_icon="code")

    data = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(teacher)).json()
    assert data["cover_source"] == cover_generator.SOURCE_FALLBACK
    assert data["cover_image"]


def test_generation_skips_openai_when_org_budget_is_spent(client, teacher, storage, monkeypatch):
    def explode(url, kwargs):
        raise AssertionError("бюджет исчерпан — обращаться к модели нельзя")

    _patch_openai(monkeypatch, explode)
    monkeypatch.setattr(classes_router.ai_budget, "can_spend", lambda db, org: False)
    cls = _make_class(client, teacher, cover_color="teal", cover_icon="code")

    data = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(teacher)).json()
    assert data["cover_source"] == cover_generator.SOURCE_FALLBACK
    assert data["cover_image"]


def test_generation_records_token_spend(client, teacher, storage, monkeypatch, db_session):
    from models import AiUsageLog

    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(
        _png_bytes(), usage={"input_tokens": 30, "output_tokens": 272, "total_tokens": 302}))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                headers=auth_headers(teacher))

    row = (db_session.query(AiUsageLog)
           .filter(AiUsageLog.class_id == cls["id"], AiUsageLog.endpoint == "cover_image")
           .one())
    assert row.total_tokens == 302
    # Не должно съедать дневной лимит сообщений ИИ-чата.
    from services.ai_quota import CHAT_ENDPOINTS
    assert row.endpoint not in CHAT_ENDPOINTS


# ── Доступ и защита от лишних вызовов ───────────────────────────────────────
def test_only_the_owner_can_generate(client, db_session, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")

    other = make_user(db_session, role="teacher")
    resp = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(other))
    assert resp.status_code == 403


def test_students_cannot_generate(client, db_session, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")

    student = make_user(db_session, role="student")
    resp = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                       headers=auth_headers(student))
    assert resp.status_code == 403


def test_generation_is_rate_limited(client, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    classes_router._cover_limiter.reset(teacher.id)
    try:
        codes = [
            client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                        headers=auth_headers(teacher)).status_code
            for _ in range(classes_router._cover_limiter.max_calls + 1)
        ]
        assert codes[-1] == 429
        assert set(codes[:-1]) == {200}
    finally:
        classes_router._cover_limiter.reset(teacher.id)


def test_concurrent_generation_for_one_class_is_rejected(client, teacher, storage, monkeypatch):
    """Двойной тап по кнопке не должен запускать вторую оплаченную генерацию."""
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")

    classes_router._covers_in_flight.add(cls["id"])
    try:
        resp = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                           headers=auth_headers(teacher))
        assert resp.status_code == 409
    finally:
        classes_router._covers_in_flight.discard(cls["id"])


def test_in_flight_lock_is_released_after_generation(client, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                headers=auth_headers(teacher))
    assert cls["id"] not in classes_router._covers_in_flight


def test_reading_a_class_never_generates(client, teacher, storage, monkeypatch):
    """Открытие предмета/списка не должно стоить ни одной генерации."""
    def explode(url, kwargs):
        raise AssertionError("просмотр класса не должен ходить в OpenAI")

    _patch_openai(monkeypatch, explode)
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    for _ in range(3):
        assert client.get(f"/api/classes/{cls['id']}", headers=auth_headers(teacher)).status_code == 200
        assert client.get("/api/classes/", headers=auth_headers(teacher)).status_code == 200


def test_saved_cover_url_is_stable_across_reads(client, teacher, storage, monkeypatch):
    """Все студенты предмета обязаны видеть одну и ту же сохранённую обложку."""
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    generated = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                            headers=auth_headers(teacher)).json()

    for _ in range(3):
        fetched = client.get(f"/api/classes/{cls['id']}", headers=auth_headers(teacher)).json()
        assert fetched["cover_image"] == generated["cover_image"]
        assert fetched["cover_thumbnail"] == generated["cover_thumbnail"]


# ── Совместимость со старыми классами ───────────────────────────────────────
def test_legacy_class_keeps_its_uploaded_cover(client, db_session, teacher, storage):
    """Классы, созданные до новой системы, ничего не теряют: цвет с иконкой у
    них NULL, а картинка остаётся ровно та, что была."""
    from crud import classes as crud_classes

    legacy = crud_classes.create_class(
        db_session, "Старый класс", None, created_by=teacher.id,
        org_type=teacher.org_type,
        cover_image="https://cdn.test/materials/covers/old.webp",
        cover_thumbnail="https://cdn.test/materials/covers/thumbnails/old.webp",
    )
    data = client.get(f"/api/classes/{legacy.id}", headers=auth_headers(teacher)).json()
    assert data["cover_image"] == "https://cdn.test/materials/covers/old.webp"
    assert data["cover_color"] is None and data["cover_icon"] is None
    assert data["cover_source"] is None


def test_legacy_class_migrates_on_first_generation(client, db_session, teacher, storage, monkeypatch):
    from crud import classes as crud_classes

    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    legacy = crud_classes.create_class(
        db_session, "Старый класс", None, created_by=teacher.id,
        org_type=teacher.org_type,
        cover_image="https://cdn.test/materials/covers/old.webp",
    )
    data = client.post(f"/api/classes/{legacy.id}/cover/generate",
                       json={"color": "green", "icon": "dna"},
                       headers=auth_headers(teacher)).json()

    assert data["cover_color"] == "green" and data["cover_icon"] == "dna"
    assert data["cover_source"] == cover_generator.SOURCE_AI
    assert data["cover_image"] != "https://cdn.test/materials/covers/old.webp"


def test_plain_save_never_replaces_a_legacy_uploaded_cover(client, db_session, teacher, storage):
    """Форма редактирования всегда подставляет цвет и иконку по умолчанию —
    просто чтобы нарисовать пикер. Обычное «Сохранить» на старом предмете не
    должно из-за этого подменить картинку преподавателя на градиент: переход
    на новую систему делает только явная генерация."""
    from crud import classes as crud_classes

    legacy = crud_classes.create_class(
        db_session, "Старый класс", None, created_by=teacher.id,
        org_type=teacher.org_type,
        cover_image="https://cdn.test/materials/covers/old.webp",
        cover_thumbnail="https://cdn.test/materials/covers/thumbnails/old.webp",
    )
    data = client.put(f"/api/classes/{legacy.id}",
                      json={"name": "Переименован", "cover_color": "pink", "cover_icon": "note"},
                      headers=auth_headers(teacher)).json()

    assert data["cover_image"] == "https://cdn.test/materials/covers/old.webp"
    assert data["cover_thumbnail"] == "https://cdn.test/materials/covers/thumbnails/old.webp"
    assert data["name"] == "Переименован"
    # Выбранные значения всё равно сохранены — с ними пойдёт первая генерация.
    assert data["cover_color"] == "pink" and data["cover_icon"] == "note"
    assert not storage.objects, "старую обложку не должны были перерисовывать"


def test_legacy_cover_survives_repeated_saves(client, db_session, teacher, storage):
    """Сохранений может быть сколько угодно — пока преподаватель не нажал
    «Сгенерировать», загруженная им картинка остаётся на месте."""
    from crud import classes as crud_classes

    legacy = crud_classes.create_class(
        db_session, "Старый класс", None, created_by=teacher.id,
        org_type=teacher.org_type,
        cover_image="https://cdn.test/materials/covers/old.webp",
    )
    for color in ("pink", "blue", "green"):
        data = client.put(f"/api/classes/{legacy.id}", json={"cover_color": color},
                          headers=auth_headers(teacher)).json()
        assert data["cover_image"] == "https://cdn.test/materials/covers/old.webp"
        assert data["cover_color"] == color
    assert not storage.objects


def test_legacy_cover_file_is_not_deleted_by_a_plain_save(client, db_session, teacher, storage, monkeypatch):
    """И сам файл в хранилище тоже должен пережить обычное сохранение."""
    from crud import classes as crud_classes

    deleted = []
    monkeypatch.setattr("crud.classes.delete_upload_file", deleted.append)

    legacy = crud_classes.create_class(
        db_session, "Старый класс", None, created_by=teacher.id,
        org_type=teacher.org_type,
        cover_image="https://cdn.test/materials/covers/old.webp",
    )
    client.put(f"/api/classes/{legacy.id}",
               json={"name": "Переименован", "cover_color": "pink"},
               headers=auth_headers(teacher))
    assert deleted == []


def test_editing_appearance_redraws_the_cover_without_openai(client, teacher, storage, monkeypatch):
    def explode(url, kwargs):
        raise AssertionError("смена цвета не должна сама запускать генерацию")

    _patch_openai(monkeypatch, explode)
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")

    resp = client.put(f"/api/classes/{cls['id']}", json={"cover_color": "pink"},
                      headers=auth_headers(teacher))
    data = resp.json()
    assert data["cover_color"] == "pink"
    assert data["cover_icon"] == "atom"
    assert data["cover_image"] != cls["cover_image"]
    assert data["cover_source"] == cover_generator.SOURCE_FALLBACK


def test_editing_other_fields_leaves_the_cover_alone(client, teacher, storage, monkeypatch):
    _patch_openai(monkeypatch, lambda url, kw: _openai_image_response(_png_bytes()))
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    generated = client.post(f"/api/classes/{cls['id']}/cover/generate", json={},
                            headers=auth_headers(teacher)).json()

    data = client.put(f"/api/classes/{cls['id']}", json={"name": "Новое имя"},
                      headers=auth_headers(teacher)).json()
    assert data["name"] == "Новое имя"
    assert data["cover_image"] == generated["cover_image"]
    assert data["cover_source"] == cover_generator.SOURCE_AI


def test_legacy_client_can_still_send_an_uploaded_cover(client, teacher, storage):
    """Уже установленные у пользователей старые сборки приложения шлют
    cover_image при каждом сохранении — они не должны сломаться."""
    cls = _make_class(client, teacher, cover_color="blue", cover_icon="atom")
    data = client.put(f"/api/classes/{cls['id']}",
                      json={"name": "Класс", "cover_image": "https://cdn.test/legacy.png"},
                      headers=auth_headers(teacher)).json()
    assert data["cover_image"] == "https://cdn.test/legacy.png"
