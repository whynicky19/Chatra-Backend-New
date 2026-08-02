"""HEIC/HEIF — дефолтный формат фото на iPhone (Settings > Camera > Formats >
"High Efficiency", включено по умолчанию на большинстве устройств). Клиент
(lib/utils/upload_limits.dart::kAllowedExtensions) давно разрешает выбрать
такой файл в пикере — а бэкенд полностью отклонял загрузку (ALLOWED_EXTENSIONS
не знал про heic/heif), студент с iPhone не мог сдать фото домашки вообще,
пока сам не сконвертировал файл. Плюс: даже разрешив загрузку, OpenAI vision
не принимает image/heic — нужна конвертация в JPEG перед отправкой модели."""
import asyncio
import io

import pytest
from PIL import Image

from routers.uploads import _validate_file_content, ALLOWED_EXTENSIONS
from services import ai_grader


def _heic_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), color=color).save(buf, format="HEIF")
    return buf.getvalue()


# ── Загрузка: расширение разрешено, magic bytes проверяются ─────────────────

def test_heic_extension_is_allowed_for_upload():
    assert "heic" in ALLOWED_EXTENSIONS
    assert "heif" in ALLOWED_EXTENSIONS


def test_validate_file_content_accepts_real_heic_bytes():
    assert _validate_file_content(_heic_bytes(), "heic") is True


def test_validate_file_content_rejects_fake_heic_bytes():
    """Расширение .heic, но содержимое — не HEIF-контейнер (например,
    переименованный .txt) — должно отбиваться, как и для остальных форматов."""
    assert _validate_file_content(b"this is not a heic file" * 4, "heic") is False


def test_validate_file_content_rejects_truncated_heic():
    assert _validate_file_content(b"\x00\x00\x00", "heic") is False


# ── Конвертация HEIC -> JPEG (нужна для OpenAI vision) ──────────────────────

def test_heic_to_jpeg_converts_real_heic():
    jpeg = ai_grader._heic_to_jpeg(_heic_bytes())
    assert jpeg is not None
    assert jpeg[:3] == b"\xff\xd8\xff"  # JPEG magic bytes


def test_heic_to_jpeg_returns_none_for_garbage():
    assert ai_grader._heic_to_jpeg(b"not a heic file at all") is None


def test_heic_to_jpeg_applies_exif_orientation_rotation():
    """Регресс: без ImageOps.exif_transpose портретное фото телефона
    (физически landscape-пиксели + EXIF orientation=6, "повернуть на 90°
    при показе") конвертировалось БЕЗ применения поворота — на выходе
    получалась картинка, развёрнутая набок относительно того, что видел
    человек, а тег ориентации в процессе конвертации терялся."""
    img = Image.new("RGB", (100, 50), color=(255, 0, 0))  # 100×50 "landscape" пиксели
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: повернуть на 90° по часовой при показе
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())

    converted = ai_grader._heic_to_jpeg(buf.getvalue())
    assert converted is not None
    result = Image.open(io.BytesIO(converted))
    # После правильно применённого поворота размеры меняются местами: 50×100.
    assert result.size == (50, 100)


# ── _fetch_image_data_uri: HEIC скачивается и конвертируется в JPEG ─────────

class _FakeGetClient:
    def __init__(self, content: bytes, content_type: str):
        self._content = content
        self._content_type = content_type

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        class _R:
            is_success = True
            status_code = 200
            content = self._content
            headers = {"content-type": self._content_type}
        return _R()


def test_fetch_image_data_uri_converts_heic_to_jpeg(monkeypatch):
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _FakeGetClient(_heic_bytes(), "image/heic"),
    )
    uri = asyncio.run(ai_grader._fetch_image_data_uri("http://localhost:8000/api/uploads/photo.heic"))
    assert uri is not None
    # OpenAI vision не принимает image/heic — на выходе должен быть jpeg.
    assert uri.startswith("data:image/jpeg;base64,")


def test_fetch_image_data_uri_regular_jpeg_unaffected(monkeypatch):
    """Обычный jpeg не должен внезапно начать конвертироваться/портиться."""
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="JPEG")
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _FakeGetClient(buf.getvalue(), "image/jpeg"),
    )
    uri = asyncio.run(ai_grader._fetch_image_data_uri("http://localhost:8000/api/uploads/photo.jpg"))
    assert uri.startswith("data:image/jpeg;base64,")


def test_fetch_image_data_uri_broken_heic_returns_none(monkeypatch):
    monkeypatch.setattr(
        ai_grader.httpx, "AsyncClient",
        _FakeGetClient(b"garbage not heic", "image/heic"),
    )
    uri = asyncio.run(ai_grader._fetch_image_data_uri("http://localhost:8000/api/uploads/photo.heic"))
    assert uri is None


# ── IMAGE_EXTS: heic должен идти vision-путём, а не text-путём ──────────────

def test_heic_is_in_shared_image_exts():
    assert "heic" in ai_grader.IMAGE_EXTS
    assert "heif" in ai_grader.IMAGE_EXTS
