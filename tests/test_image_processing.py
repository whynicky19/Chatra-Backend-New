"""Юнит-тесты сжатия обложек классов (ресайз + WebP) — см.
services/image_processing.py. Цель: подтвердить требования оптимизации
(макс. ширина 1600/480px, сохранение пропорций и альфа-канала, отсутствие
апскейла) без обращения к сети/R2."""
import io

import pytest
from PIL import Image

from services.image_processing import (
    MAIN_MAX_WIDTH,
    THUMBNAIL_MAX_WIDTH,
    process_cover_image,
)


def _png_bytes(size, mode="RGB", color=(10, 20, 30)) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_resized_to_max_width_preserving_aspect_ratio():
    raw = _png_bytes((4000, 2000))
    main_bytes, main_ext, thumb_bytes, thumb_ext = process_cover_image(raw)

    main_img = Image.open(io.BytesIO(main_bytes))
    assert main_img.width == MAIN_MAX_WIDTH
    assert main_img.height == MAIN_MAX_WIDTH // 2  # 4000x2000 is 2:1

    thumb_img = Image.open(io.BytesIO(thumb_bytes))
    assert thumb_img.width == THUMBNAIL_MAX_WIDTH
    assert thumb_img.height == THUMBNAIL_MAX_WIDTH // 2


def test_small_image_is_not_upscaled():
    raw = _png_bytes((300, 150))
    main_bytes, _, thumb_bytes, _ = process_cover_image(raw)

    assert Image.open(io.BytesIO(main_bytes)).size == (300, 150)
    assert Image.open(io.BytesIO(thumb_bytes)).size == (300, 150)


def test_output_is_webp_by_default():
    raw = _png_bytes((2000, 1000))
    main_bytes, main_ext, thumb_bytes, thumb_ext = process_cover_image(raw)
    assert main_ext == "webp"
    assert thumb_ext == "webp"
    assert Image.open(io.BytesIO(main_bytes)).format == "WEBP"
    assert Image.open(io.BytesIO(thumb_bytes)).format == "WEBP"


def test_alpha_channel_is_preserved():
    raw = _png_bytes((1000, 500), mode="RGBA", color=(10, 20, 30, 128))
    main_bytes, main_ext, _, _ = process_cover_image(raw)
    result = Image.open(io.BytesIO(main_bytes))
    assert result.mode in ("RGBA", "LA")
    assert main_ext == "webp"


def _photo_like_png_bytes(size) -> bytes:
    """Размытые случайные блоки — по частотному спектру ближе к настоящему
    фото, чем чистый шум (который PNG жмёт лучше любого lossy-кодека и делает
    сравнение WebP/PNG нерепрезентативным)."""
    import random
    from PIL import ImageFilter

    rng = random.Random(1)
    w, h = size
    step = 40
    img = Image.new("RGB", size)
    px = img.load()
    colors = {
        (bx, by): (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for bx in range(0, w, step) for by in range(0, h, step)
    }
    for x in range(w):
        for y in range(h):
            px[x, y] = colors[((x // step) * step, (y // step) * step)]
    img = img.filter(ImageFilter.GaussianBlur(30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compression_meaningfully_reduces_size():
    raw = _photo_like_png_bytes((2400, 1600))
    main_bytes, _, _, _ = process_cover_image(raw)
    assert len(main_bytes) < len(raw) * 0.5


def test_thumbnail_is_smaller_than_main():
    raw = _png_bytes((3000, 3000))
    main_bytes, _, thumb_bytes, _ = process_cover_image(raw)
    assert len(thumb_bytes) < len(main_bytes)


def test_invalid_image_bytes_raise():
    with pytest.raises(Exception):
        process_cover_image(b"not an image")
