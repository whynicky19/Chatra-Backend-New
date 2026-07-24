"""Сжатие обложек классов: ресайз + перекодирование в WebP.

Экономит и вес (WebP при том же визуальном качестве обычно в 5-10 раз легче
исходного PNG/JPEG со смартфона), и время загрузки в браузере — крупная
обложка с телефона (3000-4000px, несколько МБ) ужимается до ≤1600px по
ширине и ≤~150-300 КБ. Отдельно готовится миниатюра для карточек списков.
"""
import io
import logging

logger = logging.getLogger(__name__)

MAIN_MAX_WIDTH = 1600
THUMBNAIL_MAX_WIDTH = 480
WEBP_QUALITY = 82
JPEG_QUALITY = 85


def _resize(img, max_width: int):
    if img.width <= max_width:
        return img
    ratio = max_width / img.width
    new_size = (max_width, max(1, round(img.height * ratio)))
    from PIL import Image
    return img.resize(new_size, Image.LANCZOS)


def _encode(img, *, quality: int) -> tuple[bytes, str]:
    """Кодирует в WebP; если сборка Pillow без libwebp — фолбэк на JPEG
    (с проекцией альфа-канала на белый фон, JPEG прозрачность не умеет)."""
    buf = io.BytesIO()
    try:
        img.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue(), "webp"
    except Exception:
        logger.warning("Кодирование в WebP не удалось, фолбэк на JPEG", exc_info=True)
        buf = io.BytesIO()
        flat = img
        if img.mode in ("RGBA", "LA", "P"):
            from PIL import Image
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            flat = background
        elif img.mode != "RGB":
            flat = img.convert("RGB")
        flat.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), "jpg"


def process_cover_image(raw: bytes) -> tuple[bytes, str, bytes, str]:
    """Возвращает (main_bytes, main_ext, thumb_bytes, thumb_ext).

    Ресайз сохраняет пропорции (только уменьшение, апскейл не делаем — если
    исходник уже меньше целевой ширины, остаётся как есть). EXIF/метаданные
    не переносятся в результат — Pillow re-encode их не копирует, если не
    просить об этом явно."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        # EXIF-ориентацию нужно применить ДО ресайза, иначе повёрнутые с
        # телефона фото лягут боком — Pillow save() ориентацию не читает.
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)

        main_img = _resize(img, MAIN_MAX_WIDTH)
        main_bytes, main_ext = _encode(main_img, quality=WEBP_QUALITY)

        thumb_img = _resize(img, THUMBNAIL_MAX_WIDTH)
        thumb_bytes, thumb_ext = _encode(thumb_img, quality=WEBP_QUALITY)

    return main_bytes, main_ext, thumb_bytes, thumb_ext
