
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas
from db import get_db
from deps import get_current_teacher, get_current_user
from models import AvatarLecture, AvatarLectureSlide, TeacherAvatar
from services import slide_extractor
from services.lecture_generator import estimate_cost_usd

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/avatars", tags=["Teacher Avatars"])

ALLOWED_STYLES = {"school", "university", "professional"}
# Языки с надёжным TTS в eleven_multilingual_v2. Казахского там нет: язык без
# поддержки добавлять нельзя — озвучка выйдет ломаной.
ALLOWED_LANGUAGES = {"ru", "en"}


@router.get("/me", response_model=Optional[schemas.TeacherAvatarResponse])
def get_my_avatar(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    avatar = db.query(TeacherAvatar).filter(TeacherAvatar.teacher_id == current_user.id).first()
    return avatar


@router.post("/me", response_model=schemas.TeacherAvatarResponse, status_code=status.HTTP_201_CREATED)
def create_my_avatar(
    body: schemas.TeacherAvatarCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    existing = db.query(TeacherAvatar).filter(TeacherAvatar.teacher_id == current_user.id).first()
    if existing and existing.status in ("pending", "approved"):
        raise HTTPException(
            status_code=409,
            detail="У вас уже есть аватар или заявка на рассмотрении.",
        )

    if existing:
        existing.display_name = body.display_name or current_user.full_name
        existing.photo_url = body.photo_url
        existing.voice_sample_url = body.voice_sample_url
        existing.status = "pending"
        existing.rejection_reason = None
        existing.reviewed_by = None
        existing.reviewed_at = None
        db.commit()
        db.refresh(existing)
        _notify_admins_avatar_request(current_user)
        return existing

    avatar = TeacherAvatar(
        teacher_id=current_user.id,
        org_type=current_user.org_type,
        display_name=body.display_name or current_user.full_name,
        photo_url=body.photo_url,
        voice_sample_url=body.voice_sample_url,
        status="pending",
    )
    db.add(avatar)
    db.commit()
    db.refresh(avatar)
    _notify_admins_avatar_request(current_user)
    return avatar


def _notify_admins_avatar_request(teacher) -> None:
    """Пуш админам организации о новой заявке на аватар — она ждёт ручного
    одобрения в админке."""
    from services.fcm import notify_admins
    name = teacher.full_name or teacher.email
    notify_admins(
        teacher.org_type,
        "Заявка на аватар",
        f"{name} подал(а) заявку на создание аватара",
        {"type": "avatar_request", "notif_key": f"avatar:{teacher.id}"},
    )


def _get_approved_avatar_or_404(db: Session, teacher_id: int) -> TeacherAvatar:
    avatar = db.query(TeacherAvatar).filter(TeacherAvatar.teacher_id == teacher_id).first()
    if not avatar:
        raise HTTPException(status_code=404, detail="Сначала создайте своего аватара")
    if avatar.status != "approved":
        raise HTTPException(
            status_code=403,
            detail="Аватар ещё не одобрен администратором.",
        )
    return avatar


@router.post(
    "/lectures",
    response_model=schemas.AvatarLectureFullResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_lecture(
    body: schemas.AvatarLectureCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    avatar = _get_approved_avatar_or_404(db, current_user.id)

    if body.style not in ALLOWED_STYLES:
        raise HTTPException(status_code=422, detail=f"style должен быть одним из {sorted(ALLOWED_STYLES)}")
    if body.language not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=422, detail=f"language должен быть одним из {sorted(ALLOWED_LANGUAGES)}")
    if body.duration_minutes < 5 or body.duration_minutes > 180:
        raise HTTPException(status_code=422, detail="Длительность лекции должна быть от 5 до 180 минут")

    from services.url_safety import is_safe_fetch_url
    if not is_safe_fetch_url(body.source_file_url):
        raise HTTPException(status_code=400, detail="Недопустимый URL файла материала")

    import httpx
    async with httpx.AsyncClient(timeout=60.0) as client:
        file_resp = await client.get(body.source_file_url)
    if not file_resp.is_success:
        raise HTTPException(status_code=400, detail="Не удалось загрузить файл материала по указанному URL")

    filename = body.source_filename or body.source_file_url.split("/")[-1]
    try:
        # extract_slides шеллит soffice/pdftoppm и парсит презентацию — тяжёлая
        # блокирующая работа. В async-обработчике её нельзя звать напрямую: она
        # заморозит event loop и все параллельные запросы. Выносим в пул потоков.
        from starlette.concurrency import run_in_threadpool
        slides_data = await run_in_threadpool(
            slide_extractor.extract_slides, file_resp.content, filename
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    except Exception:
        logger.exception("Ошибка извлечения слайдов из %s", filename)
        raise HTTPException(status_code=422, detail="Не удалось распарсить файл материала")

    estimated_chars = sum(len(s.text) for s in slides_data) * 2
    estimated_cost = estimate_cost_usd(estimated_chars)

    lecture = AvatarLecture(
        avatar_id=avatar.id,
        class_id=body.class_id,
        created_by=current_user.id,
        org_type=current_user.org_type,
        title=body.title,
        source_filename=filename,
        source_file_url=body.source_file_url,
        duration_minutes=body.duration_minutes,
        style=body.style,
        language=body.language,
        auto_summary=body.auto_summary,
        status="pending_approval",
        estimated_chars=estimated_chars,
        estimated_cost_usd=estimated_cost,
    )
    db.add(lecture)
    db.flush()

    for s in slides_data:
        db.add(AvatarLectureSlide(
            lecture_id=lecture.id,
            slide_index=s.index,
            slide_image_url=s.image_url,
            slide_source_text=s.text,
        ))

    db.commit()
    db.refresh(lecture)

    # Лекция рождается в pending_approval и ждёт одобрения в админке.
    from services.fcm import notify_admins
    name = current_user.full_name or current_user.email
    notify_admins(
        current_user.org_type,
        "Лекция на модерации",
        f"{name}: «{lecture.title}»",
        {"type": "lecture_request", "notif_key": f"lecture:{lecture.id}", "lecture_id": lecture.id},
    )
    return lecture


@router.get("/lectures/mine", response_model=List[schemas.AvatarLectureResponse])
def list_my_lectures(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    return (
        db.query(AvatarLecture)
        .filter(AvatarLecture.created_by == current_user.id)
        .order_by(AvatarLecture.created_at.desc())
        .all()
    )


@router.get("/lectures/class/{class_id}", response_model=List[schemas.AvatarLectureResponse])
def list_class_lectures(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = db.query(AvatarLecture).filter(
        AvatarLecture.class_id == class_id,
        AvatarLecture.org_type == current_user.org_type,
    )
    if current_user.role == "student":
        q = q.filter(AvatarLecture.status == "ready")
    return q.order_by(AvatarLecture.created_at.desc()).all()


@router.get("/lectures/{lecture_id}/full", response_model=schemas.AvatarLectureFullResponse)
def get_lecture_full(
    lecture_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    lecture = db.query(AvatarLecture).filter(AvatarLecture.id == lecture_id).first()
    if not lecture or lecture.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Лекция не найдена")

    is_owner = lecture.created_by == current_user.id
    if current_user.role == "student" and lecture.status != "ready":
        raise HTTPException(status_code=404, detail="Лекция не найдена")
    if current_user.role == "teacher" and not is_owner and lecture.status != "ready":
        raise HTTPException(status_code=404, detail="Лекция не найдена")

    return lecture


@router.delete("/lectures/{lecture_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lecture(
    lecture_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_teacher),
):
    lecture = db.query(AvatarLecture).filter(AvatarLecture.id == lecture_id).first()
    if not lecture or lecture.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Лекция не найдена")
    if lecture.created_by != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Нет доступа")
    db.delete(lecture)
    db.commit()
