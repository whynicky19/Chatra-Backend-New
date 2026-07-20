
import logging

import httpx
from sqlalchemy.orm import Session

from db import get_session_for_org
from models import AvatarLecture, AvatarLectureSlide, TeacherAvatar
from services import elevenlabs_client, did_client, lecture_generator, slide_extractor
from services.url_safety import is_safe_fetch_url

logger = logging.getLogger(__name__)

INTRO_VIDEO_SLIDE_INDEX = 0


async def _ensure_slide_images(db: Session, lecture: AvatarLecture, slides: list) -> str | None:
    """Гарантирует, что у слайдов есть картинки презентации. Возвращает None
    при успехе или текст ошибки для пользователя, если презентацию отрендерить
    не удалось — в этом случае генерацию нужно останавливать, а не выпускать
    «готовую» лекцию, в которой аватар читает текст на фоне вечного
    «презентация готовится».

    Если рендер при создании лекции не удался (например, не было LibreOffice/
    poppler), дорендериваем здесь из исходного файла."""
    if all(s.slide_image_url for s in slides):
        return None
    if not lecture.source_file_url or not is_safe_fetch_url(lecture.source_file_url):
        logger.warning("Лекция %s: нет безопасного source_file_url для рендера слайдов", lecture.id)
        return "Исходный файл лекции недоступен — не из чего отрендерить презентацию"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(lecture.source_file_url)
        if not resp.is_success:
            logger.warning("Лекция %s: не удалось скачать исходник для рендера слайдов", lecture.id)
            return "Не удалось скачать исходный файл лекции для рендера презентации"
        filename = lecture.source_filename or lecture.source_file_url.split("/")[-1]
        images = slide_extractor.render_slide_images(resp.content, filename, count=len(slides))
    except Exception:
        logger.exception("Лекция %s: ошибка рендера картинок слайдов", lecture.id)
        return "Ошибка рендера презентации"

    for slide, img in zip(slides, images):
        if img and not slide.slide_image_url:
            slide.slide_image_url = img
    db.commit()

    if not all(s.slide_image_url for s in slides):
        return (
            "Не удалось отрендерить слайды презентации — проверьте, что на сервере "
            "установлены LibreOffice (soffice) и poppler (pdftoppm)"
        )
    return None


async def run_lecture_generation(lecture_id: int, org_type: str = "university") -> None:

    db: Session = get_session_for_org(org_type)
    try:
        lecture = db.query(AvatarLecture).filter(AvatarLecture.id == lecture_id).first()
        if not lecture:
            logger.error("AvatarLecture %s не найдена для генерации", lecture_id)
            return

        lecture.status = "generating"
        db.commit()

        avatar = db.query(TeacherAvatar).filter(TeacherAvatar.id == lecture.avatar_id).first()
        slides = (
            db.query(AvatarLectureSlide)
            .filter(AvatarLectureSlide.lecture_id == lecture.id)
            .order_by(AvatarLectureSlide.slide_index)
            .all()
        )

        if not slides:
            lecture.status = "failed"
            lecture.error_message = "Не найдено слайдов для генерации"
            db.commit()
            return

        # Слайды рендерим первыми: без них «готовая» лекция висит в «презентация
        # готовится», а деньги на TTS уже потрачены.
        slide_images_error = await _ensure_slide_images(db, lecture, slides)
        if slide_images_error:
            lecture.status = "failed"
            lecture.error_message = slide_images_error
            db.commit()
            return

        slide_texts = [s.slide_source_text or "" for s in slides]

        try:
            narrations = await lecture_generator.generate_lecture_narration(
                slide_texts=slide_texts,
                duration_minutes=lecture.duration_minutes,
                style=lecture.style,
                lecture_title=lecture.title,
                language=lecture.language or "en",
            )
        except Exception as exc:
            logger.exception("Ошибка генерации текста лекции %s", lecture_id)
            lecture.status = "failed"
            lecture.error_message = f"Ошибка генерации текста: {exc}"
            db.commit()
            return

        for slide, narration in zip(slides, narrations):
            slide.narration_text = narration

        first_audio_url = None
        for i, slide in enumerate(slides):
            text = (slide.narration_text or "").strip()
            if not text:
                continue
            try:
                audio_bytes, _ctype = await elevenlabs_client.synthesize_speech(
                    text=text,
                    voice_id=avatar.elevenlabs_voice_id if avatar else None,
                )
                audio_url = elevenlabs_client.save_audio_file(audio_bytes, prefix=f"lecture{lecture.id}_slide{i}")
                slide.audio_url = audio_url
                if i == INTRO_VIDEO_SLIDE_INDEX:
                    first_audio_url = audio_url
            except elevenlabs_client.VoiceServiceNotConfigured as exc:
                logger.warning("ElevenLabs не настроен, слайд %s остаётся без аудио: %s", i, exc)
            except Exception as exc:
                logger.exception("Ошибка озвучки слайда %s лекции %s", i, lecture_id)
                slide.audio_url = None

        db.commit()


        if avatar and avatar.photo_url and first_audio_url:
            try:
                video_url = await did_client.create_talking_intro(
                    photo_url=avatar.photo_url,
                    audio_url=first_audio_url,
                )
                lecture.intro_video_url = video_url
            except did_client.VideoServiceNotConfigured as exc:
                logger.info("D-ID не настроен, видео-интро пропущено: %s", exc)
            except Exception:
                logger.exception("Ошибка генерации видео-интро лекции %s", lecture_id)

        if lecture.auto_summary:
            try:
                summary = await lecture_generator.generate_lecture_summary(
                    narrations=[s.narration_text or "" for s in slides],
                    lecture_title=lecture.title,
                    style=lecture.style,
                    language=lecture.language or "en",
                )
                lecture.summary_text = summary
            except Exception:
                logger.exception("Ошибка генерации конспекта лекции %s", lecture_id)

        lecture.status = "ready"
        db.commit()

    except Exception:
        logger.exception("Непредвиденная ошибка генерации лекции %s", lecture_id)
        try:
            lecture = db.query(AvatarLecture).filter(AvatarLecture.id == lecture_id).first()
            if lecture:
                lecture.status = "failed"
                lecture.error_message = "Внутренняя ошибка генерации"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
