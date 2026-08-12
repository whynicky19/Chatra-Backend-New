"""Регрессия: magic-byte проверка (routers/uploads.py::_validate_file_content)
раньше пропускала любой RIFF-файл как webp (без проверки тега формата) и
принимала mp3/wav/ogg/webm/mp4/m4a вообще без проверки содержимого — файл с
неправильной сигнатурой мог сохраниться под расширением, под которым
проигрыватель/просмотрщик его не откроет."""
from routers.uploads import _validate_file_content


def test_webp_requires_webp_tag_not_just_riff_container():
    wav_bytes = b"RIFF____WAVEfmt "
    assert _validate_file_content(wav_bytes, "webp") is False
    webp_bytes = b"RIFF____WEBPVP8 "
    assert _validate_file_content(webp_bytes, "webp") is True


def test_wav_requires_wave_tag():
    assert _validate_file_content(b"RIFF____WAVEfmt ", "wav") is True
    assert _validate_file_content(b"RIFF____WEBPVP8 ", "wav") is False
    assert _validate_file_content(b"not a riff file at all", "wav") is False


def test_ogg_requires_oggs_signature():
    assert _validate_file_content(b"OggS\x00\x02...", "ogg") is True
    assert _validate_file_content(b"not ogg", "ogg") is False


def test_webm_requires_ebml_signature():
    assert _validate_file_content(b"\x1a\x45\xdf\xa3\x00\x00", "webm") is True
    assert _validate_file_content(b"not webm at all!", "webm") is False


def test_mp4_and_m4a_require_ftyp_box():
    assert _validate_file_content(b"\x00\x00\x00\x18ftypmp42", "mp4") is True
    assert _validate_file_content(b"\x00\x00\x00\x18ftypM4A ", "m4a") is True
    assert _validate_file_content(b"not an mp4 container!!", "mp4") is False


def test_mp3_accepts_id3_tag_or_frame_sync():
    assert _validate_file_content(b"ID3\x03\x00\x00\x00", "mp3") is True
    assert _validate_file_content(bytes([0xFF, 0xFB, 0x90, 0x00]), "mp3") is True
    assert _validate_file_content(b"not an mp3 at all!", "mp3") is False
