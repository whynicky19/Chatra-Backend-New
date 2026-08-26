"""Генерация фона обложки класса: OpenAI Images API → хранилище.

Ключ OpenAI живёт только здесь и в routers/ai.py — на клиент он не уезжает
никогда, генерация возможна исключительно через бэкенд.

Модель отвечает ТОЛЬКО за фон. Главный символ предмета не входит в картинку —
его рисуют клиенты нативным компонентом поверх (см. докстринг
services/cover_art.py).

Модель: gpt-image-1.5, quality="medium", size=1536x1024. Заметно дороже
прежней gpt-image-1-mini (~$0.06 против ~$0.015 за обложку), но обложка —
это тонкая графика на тёмном градиенте, и mini на ней срывалась: грязные
линии, шумные края и «пятна» вместо мягкого свечения. Генерация происходит
только по явной кнопке, поэтому цена управляемая.
Размер — самый широкий ландшафт, который принимает API (3:2); до кадра 16:9
картинку доводит cover_art.fit_cover_frame.
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

DEFAULT_MODEL = "gpt-image-1.5"
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


async def _request_background(color: str, icon: str, seed: int,
                              subject: str | None = None) -> tuple[bytes | None, dict]:
    """Фон от OpenAI. Возвращает (png_bytes|None, usage).

    subject — название класса: по нему модель подбирает тематику фона.

    Исключения наружу не летят: единственный смысл сбоя здесь — «идём в
    фолбэк», и вызывающему коду не нужно знать, что именно сломалось.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, {}

    payload = {
        "model": _model(),
        "prompt": cover_art.build_prompt(color, icon, seed, subject=subject),
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


def _compose_and_store(background: bytes | None, color: str, icon: str,
                       seed: int, subject: str | None = None) -> tuple[str, str, str]:
    """Приводит фон к кадру 16:9 и кладёт его в хранилище.

    Символ сюда не подмешивается: картинка — это чистый фон, поверх которого
    клиенты рисуют главный символ предмета своим компонентом. Байты от модели
    всё равно проходят через Pillow — так они нормализуются (и заодно
    проверяются на пригодность) ровно тем же путём, что и локальный фолбэк.

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
                # API отдаёт 3:2, обложка живёт в 16:9 — кроп симметричный,
                # центр с символом не трогается (cover_art.fit_cover_frame).
                # Экспозицию доводим арифметикой: промпт просит тусклый ровный
                # свет, но соблюдает его модель через раз, а рядом в каталоге
                # обложки должны выглядеть одинаково (cover_art.normalize_exposure).
                image = cover_art.normalize_exposure(
                    cover_art.fit_cover_frame(bg.convert("RGB")))
            source = SOURCE_AI
        except Exception:
            # Модель ответила, но байты не открылись — это не повод оставить
            # класс без обложки, просто идём в фолбэк.
            logger.exception("cover: не удалось открыть картинку от OpenAI")
            image = None

    if image is None:
        image = cover_art.render_fallback_cover(color, seed=seed, subject=subject)

    stored = store_cover_bytes(cover_art.encode_png(image))
    if stored is None:
        # Хранилище недоступно — фолбэк здесь уже не спасёт (его тоже некуда
        # положить), поэтому это единственный случай, когда наверх летит ошибка.
        raise CoverStorageError("не удалось сохранить обложку в хранилище")
    return stored[0], stored[1], source


async def build_cover(color: str, icon: str, seed: int | None = None,
                      allow_ai: bool = True,
                      subject: str | None = None) -> tuple[str, str, str, dict]:
    """Полный цикл: промпт → OpenAI → кадр 16:9 → хранилище.

    subject — название класса. Преподаватель по-прежнему выбирает только цвет
    и символ; название уезжает в промпт подсказкой темы, а тематические
    элементы фона модель подбирает по нему сама (см. cover_art.build_prompt).

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
        background, usage = await _request_background(color, icon, seed, subject=subject)
    else:
        background, usage = None, {}
    # Pillow и загрузка в R2 блокируют — в async-эндпоинте они встали бы
    # поперёк event loop и подвесили остальные запросы воркера.
    cover_url, thumb_url, source = await run_in_threadpool(
        _compose_and_store, background, color, icon, seed, subject
    )
    return cover_url, thumb_url, source, usage


def build_fallback_cover(color: str, icon: str, seed: int | None = None,
                         subject: str | None = None) -> tuple[str, str, str]:
    """Только локальный рендер, без обращения к OpenAI (~30 мс).

    Используется при создании класса: обложка есть сразу и бесплатно, а
    AI-версию клиент запрашивает следующим шагом явной кнопкой. Так класс
    не может появиться без обложки, даже если генерацию потом отменят.

    subject — название класса: попадает в render_background и подбирает
    тематическую сцену.
    """
    return _compose_and_store(None, cover_art.normalize_color(color),
                              cover_art.normalize_icon(icon), seed or 0,
                              subject=subject)
