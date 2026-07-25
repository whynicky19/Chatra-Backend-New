import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from models import User, AiUsageLog
from crud import classes as crud_classes
import schemas
from schemas import UserCreate, UserResponse
from deps import get_current_admin
from db import get_db
from crud import users as crud_users
from security import hash_password
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(get_current_admin)]
)

@router.post("/users", response_model=UserResponse)
def create_user(
    user: UserCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    email = user.email.strip().lower()
    existing = crud_users.get_user_by_email(db, email, current_user.org_type)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    hashed_password = hash_password(user.password)

    db_user = User(
        email=email,
        hashed_password=hashed_password,
        role=user.role,
        org_type=current_user.org_type,
        # Аккаунты, созданные админом, сразу подтверждены — верификация не нужна.
        is_verified=True,
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    return db_user

@router.get("/users", response_model=list[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    return db.query(User).filter(User.org_type == current_user.org_type).all()

@router.put("/users/{user_id}/role")
def update_user_role(
    user_id: int,
    new_role: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    if new_role not in schemas.ALLOWED_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of {schemas.ALLOWED_ROLES}",
        )

    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нет доступа")

    user.role = new_role
    db.commit()

    return {"message": "Role updated"}

@router.put("/users/{user_id}/block")
def block_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нет доступа")

    user.is_active = False
    db.commit()

    return {"message": "User blocked"}

@router.put("/users/{user_id}/unblock")
def unblock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нет доступа")

    user.is_active = True
    db.commit()

    return {"message": "User unblocked"}

@router.put("/users/{user_id}/ai_unlimited")
def set_ai_unlimited(
    user_id: int,
    body: schemas.AiUnlimitedUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нет доступа")

    user.ai_unlimited = body.unlimited
    db.commit()

    return {"message": "AI limit updated", "ai_unlimited": user.ai_unlimited}

@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нет доступа")

    from services.file_cleanup import delete_user_files
    delete_user_files(user)
    db.delete(user)
    db.commit()

    return {"message": "User deleted"}

@router.get("/classes/{class_id}/members", response_model=list[UserResponse])
def get_class_members(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    obj = crud_classes.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Not found")
    return crud_classes.get_members(db, class_id)

@router.get("/ai-usage")
def get_ai_usage(
    class_id: Optional[int] = Query(None, description="Filter by class, None = all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    from sqlalchemy import desc
    q = db.query(AiUsageLog).filter(AiUsageLog.org_type == current_user.org_type)
    if class_id is not None:
        q = q.filter(AiUsageLog.class_id == class_id)
    total = q.count()
    logs = (
        q.order_by(desc(AiUsageLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": l.id,
                "user_id": l.user_id,
                "class_id": l.class_id,
                "endpoint": l.endpoint,
                "prompt_tokens": l.prompt_tokens,
                "completion_tokens": l.completion_tokens,
                "total_tokens": l.total_tokens,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }

@router.get("/ai-usage/summary")
def get_ai_usage_summary(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    from sqlalchemy import func
    rows = (
        db.query(
            AiUsageLog.class_id,
            func.sum(AiUsageLog.total_tokens).label("total_tokens"),
            func.sum(AiUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(AiUsageLog.completion_tokens).label("completion_tokens"),
            func.count(AiUsageLog.id).label("request_count"),
        )
        .filter(AiUsageLog.org_type == current_user.org_type)
        .group_by(AiUsageLog.class_id)
        .all()
    )
    return [
        {
            "class_id": r.class_id,
            "total_tokens": r.total_tokens or 0,
            "prompt_tokens": r.prompt_tokens or 0,
            "completion_tokens": r.completion_tokens or 0,
            "request_count": r.request_count or 0,
        }
        for r in rows
    ]

