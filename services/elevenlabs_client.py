
import logging
import os

import httpx

from services.storage import get_storage_service

logger = logging.getLogger(__name__)

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"

DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" - стандартный голос ElevenLabs


class VoiceServiceNotConfigured(Exception):
    pass


class VoiceServiceError(Exception):
    pass


def _api_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        raise VoiceServiceNotConfigured(
            "ELEVENLABS_API_KEY не настроен на сервере. Озвучка голосом учителя недоступна, "
            "пока администратор не подключит ключ ElevenLabs."
        )
    return key


async def clone_voice_from_sample(voice_name: str, sample_bytes: bytes, sample_filename: str) -> str:

    api_key = _api_key()

    files = {"files": (sample_filename, sample_bytes, "audio/mpeg")}
    data = {"name": voice_name[:100] or "Преподаватель"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{ELEVENLABS_BASE_URL}/voices/add",
            headers={"xi-api-key": api_key},
            data=data,
            files=files,
        )

    if not resp.is_success:
        logger.error("ElevenLabs voice clone failed: %s %s", resp.status_code, resp.text[:500])
        raise VoiceServiceError(f"Не удалось клонировать голос (ElevenLabs error {resp.status_code})")

    payload = resp.json()
    voice_id = payload.get("voice_id")
    if not voice_id:
        raise VoiceServiceError("ElevenLabs не вернул voice_id")

    return voice_id


async def synthesize_speech(text: str, voice_id: str | None = None) -> tuple[bytes, str]:

    api_key = _api_key()
    voice = voice_id or DEFAULT_VOICE_ID

    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.8,
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
        )

    if not resp.is_success:
        logger.error("ElevenLabs TTS failed: %s %s", resp.status_code, resp.text[:500])
        raise VoiceServiceError(f"Не удалось синтезировать речь (ElevenLabs error {resp.status_code})")

    return resp.content, "audio/mpeg"


def save_audio_file(audio_bytes: bytes, prefix: str = "narration") -> str:
    storage = get_storage_service()
    key = storage.build_key(prefix, "mp3")
    return storage.upload(audio_bytes, key, "audio/mpeg")


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))
