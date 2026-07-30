"""Модерация UGC: жалобы, очередь модерации.

Обязательно для публикации в сторах (App Store Review Guideline 1.2 «User-
Generated Content», Google Play «User Generated Content»): нужны одновременно
подача жалобы и реакция модератора в течение 24 часов.

Клиентские вызовы — lib/services/api_service.dart, раздел «Модерация UGC».
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import schemas
from db import get_db
from deps import get_current_admin, get_current_user
from models import Assignment, Class, Posts, Report, Submission, User
from services.rate_limit import RateLimiter
from utils.time import utcnow

logger = logging.getLogger(__name__)

# Класс лекции закодирован в заголовке поста: "[LECTURE][{class_id}] Текст" —
# см. crud/posts.py:_LECTURE_TITLE_RE. Используем тот же паттерн, чтобы в
# очереди модерации показать админу «Класс → Лекция», а не голый post_id.
_LECTURE_TITLE_RE = re.compile(r"^\[LECTURE\]\[(\d+)\]\s*")


def _report_content_info(db: Session, target_type: str, target_id: int) -> dict | None:
    """Класс/название/автор объекта жалобы — для админ-панели."""
    if target_type == "post":
        post = db.query(Posts).filter(Posts.id == target_id).first()
        if not post:
            return None
        m = _LECTURE_TITLE_RE.match(post.title or "")
        class_id = int(m.group(1)) if m else None
        title = _LECTURE_TITLE_RE.sub("", post.title or "").strip() or post.title
        klass = db.query(Class).filter(Class.id == class_id).first() if class_id else None
        author = db.query(User).filter(User.id == post.user_id).first()
        return {
            "class_id": class_id,
            "class_name": klass.name if klass else None,
            "target_title": title,
            "author_id": post.user_id,
            "author_name": author.full_name if author else None,
        }
    if target_type == "assignment":
        a = db.query(Assignment).filter(Assignment.id == target_id).first()
        if not a:
            return None
        klass = db.query(Class).filter(Class.id == a.class_id).first()
        author = db.query(User).filter(User.id == a.created_by).first()
        return {
            "class_id": a.class_id,
            "class_name": klass.name if klass else None,
            "target_title": a.title,
            "author_id": a.created_by,
            "author_name": author.full_name if author else None,
        }
    if target_type == "submission":
        s = db.query(Submission).filter(Submission.id == target_id).first()
        if not s:
            return None
        a = db.query(Assignment).filter(Assignment.id == s.assignment_id).first()
        klass = db.query(Class).filter(Class.id == a.class_id).first() if a else None
        author = db.query(User).filter(User.id == s.student_id).first()
        return {
            "class_id": a.class_id if a else None,
            "class_name": klass.name if klass else None,
            "target_title": (a.title if a else None),
            "author_id": s.student_id,
            "author_name": author.full_name if author else None,
        }
    if target_type == "user":
        author = db.query(User).filter(User.id == target_id).first()
        return {
            "class_id": None,
            "class_name": None,
            "target_title": None,
            "author_id": target_id,
            "author_name": author.full_name if author else None,
        }
    return None

router = APIRouter(tags=["moderation"])
admin_router = APIRouter(
    prefix="/admin", tags=["moderation-admin"], dependencies=[Depends(get_current_admin)]
)

# Антиспам: 10 жалоб в час на пользователя (жалоба создаёт работу модератору,
# поэтому лимит на аккаунт, а не на IP).
_report_limiter = RateLimiter(max_calls=10, window_seconds=3600, namespace="reports")


def _target_exists(db: Session, target_type: str, target_id: int, org_type: str) -> bool:
    """Существует ли объект жалобы в организации жалующегося."""
    if target_type == "user":
        return (
            db.query(User.id)
            .filter(User.id == target_id, User.org_type == org_type)
            .first()
            is not None
        )
    if target_type == "post":
        from models import Posts

        return (
            db.query(Posts.id)
            .join(User, User.id == Posts.user_id)
            .filter(Posts.id == target_id, User.org_type == org_type)
            .first()
            is not None
        )
    if target_type == "assignment":
        from models import Assignment

        return (
            db.query(Assignment.id)
            .join(User, User.id == Assignment.created_by)
            .filter(Assignment.id == target_id, User.org_type == org_type)
            .first()
            is not None
        )
    if target_type == "submission":
        from models import Submission

        return (
            db.query(Submission.id)
            .join(User, User.id == Submission.student_id)
            .filter(Submission.id == target_id, User.org_type == org_type)
            .first()
            is not None
        )
    return False


@router.post("/reports", status_code=status.HTTP_201_CREATED)
def create_report(
    body: schemas.ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Подать жалобу. 409 — этот пользователь уже жаловался на этот объект
    (клиент показывает `report_already`, а не ошибку)."""
    _report_limiter.check(
        current_user.id, detail="Слишком много жалоб. Попробуйте позже."
    )

    if not _target_exists(db, body.target_type, body.target_id, current_user.org_type):
        raise HTTPException(status_code=404, detail="Объект не найден")

    comment = (body.comment or "").strip() or None
    report = Report(
        reporter_id=current_user.id,
        org_type=current_user.org_type,
        target_type=body.target_type,
        target_id=body.target_id,
        reason=body.reason,
        comment=comment,
    )
    db.add(report)
    try:
        db.commit()
    except IntegrityError:
        # Гонка двойного тапа или повторная жалоба — уникальный индекс
        # (reporter_id, target_type, target_id).
        db.rollback()
        raise HTTPException(status_code=409, detail="report_already")
    db.refresh(report)

    # Push админам: PushService._routeByType по data.type = "admin_report"
    # открывает вкладку модерации в админке.
    try:
        from services.fcm import notify_admins

        notify_admins(
            current_user.org_type,
            "Новая жалоба",
            f"{body.target_type} #{body.target_id}: {body.reason}",
            {
                "type": "admin_report",
                "report_id": str(report.id),
                "target_type": body.target_type,
                "target_id": str(body.target_id),
            },
        )
    except Exception as e:  # noqa: BLE001 — жалоба уже создана, пуш не критичен
        logger.warning("create_report: push админам не ушёл: %s", e)

    return {"id": report.id, "status": "created"}


@admin_router.get("/reports", response_model=list[schemas.ReportResponse])
def list_reports(
    open: bool = Query(True, description="true — только незакрытые жалобы"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Очередь модерации организации. Старые жалобы сверху: реагировать нужно
    в течение 24 часов, поэтому в начале списка — самые «горящие»."""
    q = db.query(Report).filter(Report.org_type == current_user.org_type)
    if open:
        q = q.filter(Report.resolved == False)  # noqa: E712
    rows = (
        q.order_by(Report.resolved.asc(), Report.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not rows:
        return []

    # Один запрос на всех жалующихся вместо User-запроса на каждую жалобу (N+1).
    reporter_ids = {r.reporter_id for r in rows}
    reporters = {
        u.id: u for u in db.query(User).filter(User.id.in_(reporter_ids)).all()
    }
    result = []
    for r in rows:
        u = reporters.get(r.reporter_id)
        info = _report_content_info(db, r.target_type, r.target_id) or {}
        result.append(
            schemas.ReportResponse(
                id=r.id,
                target_type=r.target_type,
                target_id=r.target_id,
                reason=r.reason,
                comment=r.comment,
                reporter_name=(u.full_name if u else None),
                reporter_email=(u.email if u else None),
                created_at=r.created_at,
                resolved=r.resolved,
                class_id=info.get("class_id"),
                class_name=info.get("class_name"),
                target_title=info.get("target_title"),
                author_id=info.get("author_id"),
                author_name=info.get("author_name"),
            )
        )
    return result


@admin_router.post("/reports/{report_id}/resolve")
def resolve_report(
    report_id: int,
    body: schemas.ReportResolve | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    report = (
        db.query(Report)
        .filter(Report.id == report_id, Report.org_type == current_user.org_type)
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Жалоба не найдена")

    report.resolved = True
    report.resolution = (body.action if body else None) or "dismissed"
    report.resolved_by = current_user.id
    report.resolved_at = utcnow()
    db.commit()
    return {"id": report.id, "resolved": True, "resolution": report.resolution}


def _get_open_report_or_404(db: Session, report_id: int, org_type: str) -> Report:
    report = (
        db.query(Report)
        .filter(Report.id == report_id, Report.org_type == org_type)
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Жалоба не найдена")
    return report


@admin_router.post("/reports/{report_id}/delete-content")
def delete_report_content(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Удалить лекцию/пост или задание, на которое пожаловались, и закрыть
    жалобу с resolution=content_removed."""
    report = _get_open_report_or_404(db, report_id, current_user.org_type)

    if report.target_type == "post":
        from crud import posts as crud_posts

        deleted = crud_posts.delete_post(db, report.target_id)
    elif report.target_type == "assignment":
        from crud import assignments as crud_assignments

        deleted = crud_assignments.delete_assignment(db, report.target_id)
    else:
        raise HTTPException(
            status_code=400, detail="Контент этого типа нельзя удалить отсюда"
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Контент уже удалён")

    report.resolved = True
    report.resolution = "content_removed"
    report.resolved_by = current_user.id
    report.resolved_at = utcnow()
    db.commit()
    return {"id": report.id, "resolved": True, "resolution": report.resolution}
