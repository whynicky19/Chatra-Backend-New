"""Генерация фона обложки класса: OpenAI Images API → хранилище.

Ключ OpenAI живёт только здесь и в routers/ai.py — на клиент он не уезжает
никогда, генерация возможна исключительно через бэкенд.

Модель отвечает ТОЛЬКО за фон. Предметная иконка не входит в картинку — её
рисуют клиенты нативным компонентом поверх (см. докстринг services/cover_art.py).

Модель: gpt-image-1-mini, quality="medium", size=1536x1024 — ~$0.015 за обложку.
Medium, а не low: обложка — плоская графика с чистыми линиями и ровными
краями, и на low модель заметно хуже держит композицию и аккуратность форм.
Ландшафт, а не квадрат: обложку показывают широкой полосой (карточка каталога
~2.1:1, шапка класса ~3.3:1), и от квадрата в кадр попадала только средняя
полоса — 48% и 30% высоты, из-за чего обложка выглядела пустой.
Всё переопределяется окружением (COVER_IMAGE_MODEL/COVER_IMAGE_QUALITY/
COVER_IMAGE_SIZE).

Главная гарантия модуля: build_cover() НИКОГДА не возвращает «нет обложки».
Любой сбой — нет ключа, ошибка/таймаут/модерация OpenAI, исчерпан бюджет,
битые байты — деградирует к локальному фолбэку из cover_art.render_fallback_cover
в том же визуальном языке. Пустой обложки пользователь не увидит.
"""
import base64
import logging
import os
import random

import httpx

from services import cover_art
from services.image_storage import store_cover_bytes

logger = logging.getLogger(__name__)

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"

DEFAULT_MODEL = "gpt-image-1-mini"
DEFAULT_QUALITY = "medium"
DEFAULT_SIZE = "1536x1024"

# Генерация у image-моделей заметно медленнее чата; 120 с с запасом перекрывает
# практический разброс, дальше клиенту честнее показать ошибку/фолбэк.
REQUEST_TIMEOUT = 120.0

# Источник обложки, хранится в classes.cover_source.
SOURCE_AI = "ai"              # сгенерирована моделью
SOURCE_FALLBACK = "fallback"  # локальный рендер (модель была недоступна)
SOURCE_UPLOAD = "upload"      # историческая обложка, загруженная пользователем


class CoverStorageError(Exception):
    """Картинку собрали, но не смогли положить в хранилище."""


def _model() -> str:
    return os.getenv("COVER_IMAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _quality() -> str:
    return os.getenv("COVER_IMAGE_QUALITY", DEFAULT_QUALITY).strip() or DEFAULT_QUALITY


def _size() -> str:
    return os.getenv("COVER_IMAGE_SIZE", DEFAULT_SIZE).strip() or DEFAULT_SIZE


def generation_enabled() -> bool:
    """False — ключа нет, работаем только на фолбэках (это штатный режим,
    а не ошибка: локальная разработка и тесты живут именно так)."""
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


async def _request_background(color: str, icon: str, seed: int) -> tuple[bytes | None, dict]:
    """Фон от OpenAI. Возвращает (png_bytes|None, usage).

    Исключения наружу не летят: единственный смысл сбоя здесь — «идём в
    фолбэк», и вызывающему коду не нужно знать, что именно сломалось.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, {}

    payload = {
        "model": _model(),
        "prompt": cover_art.build_prompt(color, icon, seed),
        "size": _size(),
        "quality": _quality(),
        "n": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                OPENAI_IMAGES_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
            )
    except httpx.TimeoutException:
        logger.warning("cover: таймаут генерации обложки (%s/%s)", color, icon)
        return None, {}
    except httpx.RequestError as e:
        logger.warning("cover: OpenAI недоступен (%s)", e)
        return None, {}

    if not resp.is_success:
        # Тело ответа полезно в логе: сюда попадают и отказ модерации промпта,
        # и исчерпанная квота организации, и снятая с обслуживания модель.
        logger.warning("cover: OpenAI вернул %s: %s", resp.status_code, resp.text[:400])
        return None, {}

    try:
        data = resp.json()
        b64 = data["data"][0]["b64_json"]
        raw = base64.b64decode(b64)
    except Exception:
        logger.exception("cover: не удалось разобрать ответ Images API")
        return None, {}

    if not raw:
        return None, {}
    return raw, data.get("usage") or {}


def _compose_and_store(background: bytes | None, color: str, icon: str, seed: int) -> tuple[str, str, str]:
    """Кладёт фон обложки в хранилище.

    Иконка сюда не подмешивается: картинка — это чистый фон, поверх которого
    клиенты рисуют предметную иконку своим компонентом. Байты от модели всё
    равно проходят через Pillow — так они нормализуются (и заодно проверяются
    на пригодность) ровно тем же путём, что и локальный фолбэк.

    Блокирующая (Pillow + сеть до R2) — вызывать только в пуле потоков,
    см. build_cover(). Возвращает (cover_url, thumbnail_url, source).
    """
    from PIL import Image
    import io

    source = SOURCE_FALLBACK
    image = None
    if background:
        try:
            with Image.open(io.BytesIO(background)) as bg:
                bg.load()
                image = bg.convert("RGB")
            source = SOURCE_AI
        except Exception:
            # Модель ответила, но байты не открылись — это не повод оставить
            # класс без обложки, просто идём в фолбэк.
            logger.exception("cover: не удалось открыть картинку от OpenAI")
            image = None

    if image is None:
        image = cover_art.render_fallback_cover(color, seed=seed)

    stored = store_cover_bytes(cover_art.encode_png(image))
    if stored is None:
        # Хранилище недоступно — фолбэк здесь уже не спасёт (его тоже некуда
        # положить), поэтому это единственный случай, когда наверх летит ошибка.
        raise CoverStorageError("не удалось сохранить обложку в хранилище")
    return stored[0], stored[1], source


async def build_cover(color: str, icon: str, seed: int | None = None,
                      allow_ai: bool = True) -> tuple[str, str, str, dict]:
    """Полный цикл: промпт → OpenAI → иконка поверх → хранилище.

    allow_ai=False пропускает обращение к модели и сразу собирает фолбэк —
    так вызывающий код отрабатывает исчерпанный бюджет организации, не
    оставляя пользователя без обложки и не тратя ни цента.

    Возвращает (cover_image_url, cover_thumbnail_url, source, usage), где
    usage — расход токенов от Images API для учёта в ai_usage_logs (пустой,
    если генерация не состоялась). Бросает CoverStorageError только если
    недоступно само хранилище.
    """
    from starlette.concurrency import run_in_threadpool

    color = cover_art.normalize_color(color)
    icon = cover_art.normalize_icon(icon)
    if seed is None:
        seed = random.randrange(1 << 30)

    if allow_ai:
        background, usage = await _request_background(color, icon, seed)
    else:
        background, usage = None, {}
    # Pillow и загрузка в R2 блокируют — в async-эндпоинте они встали бы
    # поперёк event loop и подвесили остальные запросы воркера.
    cover_url, thumb_url, source = await run_in_threadpool(
        _compose_and_store, background, color, icon, seed
    )
    return cover_url, thumb_url, source, usage


def build_fallback_cover(color: str, icon: str, seed: int | None = None) -> tuple[str, str, str]:
    """Только локальный рендер, без обращения к OpenAI (~30 мс).

    Используется при создании класса: обложка есть сразу и бесплатно, а
    AI-версию клиент запрашивает следующим шагом явной кнопкой. Так класс
    не может появиться без обложки, даже если генерацию потом отменят.
    """
    return _compose_and_store(None, cover_art.normalize_color(color),
                              cover_art.normalize_icon(icon), seed or 0)
