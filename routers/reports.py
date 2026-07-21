"""Жалобы пользователей (UGC-модерация, App Store Guideline 1.2).

Любой авторизованный пользователь может пожаловаться на другого пользователя
или его сообщение. Администратор видит список открытых жалоб и закрывает их.
Разработчик/модератор обязан реагировать на жалобы в течение 24 часов."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from db import get_db
from deps import get_current_user, get_current_admin
from models import Report, User

router = APIRouter(prefix="/reports", tags=["Reports"])


class ReportCreate(BaseModel):
    reported_user_id: int
    reason: str
    content: Optional[str] = None
    message_id: Optional[int] = None


class ReportResponse(BaseModel):
    id: int
    reporter_id: int
    reported_user_id: int
    reason: str
    content: Optional[str] = None
    message_id: Optional[int] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("", status_code=201)
def create_report(
    payload: ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Создать жалобу. Нельзя пожаловаться на самого себя."""
    if payload.reported_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="cannot_report_self")
    report = Report(
        reporter_id=current_user.id,
        reported_user_id=payload.reported_user_id,
        reason=(payload.reason or "other")[:64],
        content=(payload.content or None),
        message_id=payload.message_id,
        status="open",
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    # Пуш админам организации: жалоба лежит в админке, без пуша её заметят
    # только при следующем заходе.
    from services.fcm import notify_admins
    reporter = current_user.full_name or current_user.email
    notify_admins(
        current_user.org_type,
        "Новая жалоба",
        f"{reporter}: {report.reason}",
        {"type": "admin_report", "notif_key": f"report:{report.id}", "report_id": report.id},
    )
    return {"id": report.id, "status": report.status}


@router.get("")
def list_reports(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Список жалоб для админа (по умолчанию все, можно фильтр по статусу).
    Обогащаем именами/email жалобщика и нарушителя, чтобы админу было понятно."""
    q = db.query(Report)
    if status:
        q = q.filter(Report.status == status)
    rows = q.order_by(desc(Report.created_at)).limit(200).all()

    ids = {r.reporter_id for r in rows} | {r.reported_user_id for r in rows}
    users = (
        {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()}
        if ids else {}
    )

    def name_of(uid: int):
        u = users.get(uid)
        return (u.full_name or u.email) if u else None

    def active_of(uid: int):
        u = users.get(uid)
        return bool(u.is_active) if u else None

    return [
        {
            "id": r.id,
            "reporter_id": r.reporter_id,
            "reporter_name": name_of(r.reporter_id),
            "reported_user_id": r.reported_user_id,
            "reported_user_name": name_of(r.reported_user_id),
            "reported_user_active": active_of(r.reported_user_id),
            "reason": r.reason,
            "content": r.content,
            "message_id": r.message_id,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.put("/{report_id}/resolve")
def resolve_report(
    report_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Пометить жалобу как обработанную."""
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="not_found")
    report.status = "resolved"
    db.commit()
    return {"id": report.id, "status": report.status}
