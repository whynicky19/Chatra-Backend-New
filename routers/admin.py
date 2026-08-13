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

    # Собираем file_urls до удаления, физически чистим хранилище только
    # ПОСЛЕ успешного commit() — см. routers/auth.py::delete_me.
    from services.file_cleanup import delete_urls, user_file_urls
    urls = user_file_urls(user)
    db.delete(user)
    db.commit()
    delete_urls(urls)

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

def _name_maps(db: Session, logs) -> dict:
    """ФИО/почта пользователей и названия классов для строк расхода.

    Два запроса на страницу, а не по запросу на строку: дашборд показывает по
    50 записей, и ленивое обращение к relationship превращало бы это в сотню
    round-trip'ов. Класс мог быть удалён, а user_id обнулён (ON DELETE SET
    NULL) — тогда в словаре просто нет ключа и наружу уедет null, а строка
    расхода всё равно останется видимой.
    """
    from models import Class

    user_ids = {l.user_id for l in logs if l.user_id}
    class_ids = {l.class_id for l in logs if l.class_id}
    users = (db.query(User.id, User.full_name, User.email)
             .filter(User.id.in_(user_ids)).all()) if user_ids else []
    classes = (db.query(Class.id, Class.name)
               .filter(Class.id.in_(class_ids)).all()) if class_ids else []
    return {
        "users": {u.id: u.full_name for u in users},
        "emails": {u.id: u.email for u in users},
        "classes": {c.id: c.name for c in classes},
    }


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
    names = _name_maps(db, logs)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": l.id,
                "user_id": l.user_id,
                # ФИО и название класса отдаём прямо здесь: без них админка
                # показывала «user 42 / class 7» и по каждой строке дашборда
                # приходилось бы делать отдельный запрос.
                "user_name": names["users"].get(l.user_id),
                "user_email": names["emails"].get(l.user_id),
                "class_id": l.class_id,
                "class_name": names["classes"].get(l.class_id),
                "endpoint": l.endpoint,
                "label": AI_ENDPOINT_LABELS.get(l.endpoint, l.endpoint),
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


# Человекочитаемые названия видов расхода. Ключи — значения AiUsageLog.endpoint,
# которые пишут routers/ai.py (chat/chat_vision/ai_title), routers/classes.py
# (cover_image) и ИИ-проверка работ (ai-grade).
AI_ENDPOINT_LABELS = {
    "chat": "Чат с ИИ",
    "chat_vision": "Чат с ИИ (с изображением)",
    "ai_title": "Название чата",
    "cover_image": "Обложка предмета",
    "ai-grade": "Проверка работ",
}


@router.get("/ai-usage/by-endpoint")
def get_ai_usage_by_endpoint(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Расход токенов в разрезе видов запросов.

    Общая сумма по классам не отвечала на вопрос «на что именно ушли токены»:
    в неё одинаково падают и переписка с ИИ, и генерация обложек, и заголовки
    чатов. Здесь тот же расход разложен по AiUsageLog.endpoint, поэтому видно
    цену каждой функции отдельно.
    """
    from sqlalchemy import desc, func

    rows = (
        db.query(
            AiUsageLog.endpoint,
            func.sum(AiUsageLog.total_tokens).label("total_tokens"),
            func.sum(AiUsageLog.prompt_tokens).label("prompt_tokens"),
            func.sum(AiUsageLog.completion_tokens).label("completion_tokens"),
            func.count(AiUsageLog.id).label("request_count"),
        )
        .filter(AiUsageLog.org_type == current_user.org_type)
        .group_by(AiUsageLog.endpoint)
        .order_by(desc(func.sum(AiUsageLog.total_tokens)))
        .all()
    )
    return [
        {
            "endpoint": r.endpoint,
            # Неизвестный endpoint (новая функция, старые записи) показываем
            # как есть, а не прячем — иначе часть расхода пропадёт из отчёта.
            "label": AI_ENDPOINT_LABELS.get(r.endpoint, r.endpoint),
            "total_tokens": r.total_tokens or 0,
            "prompt_tokens": r.prompt_tokens or 0,
            "completion_tokens": r.completion_tokens or 0,
            "request_count": r.request_count or 0,
        }
        for r in rows
    ]


# Вид расхода, которым пишется генерация обложки (routers/classes.py).
COVER_ENDPOINT = "cover_image"


@router.get("/ai-usage/covers")
def get_cover_usage(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Расход на обложки предметов: кто сгенерировал, для какого предмета и
    сколько токенов ушло.

    Отдельный отчёт, а не фильтр по /ai-usage, потому что у обложки другой
    вопрос: не «сколько потратил пользователь», а «сколько стоила каждая
    картинка». Поэтому здесь сразу ФИО преподавателя и название предмета, а
    рядом — итог по ВСЕМ генерациям, а не только по текущей странице: именно
    его админ сверяет со счётом OpenAI.

    ФИО или название могут прийти null: пользователь удалён (user_id
    обнуляется по ON DELETE SET NULL) или класс удалён. Строку расхода это не
    прячет — иначе итог перестал бы сходиться с реальным счётом.
    """
    from sqlalchemy import desc, func

    where = (AiUsageLog.org_type == current_user.org_type,
             AiUsageLog.endpoint == COVER_ENDPOINT)
    totals = (db.query(func.count(AiUsageLog.id),
                       func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                       func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                       func.coalesce(func.sum(AiUsageLog.completion_tokens), 0))
              .filter(*where)
              .one())
    logs = (db.query(AiUsageLog)
            .filter(*where)
            .order_by(desc(AiUsageLog.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all())
    names = _name_maps(db, logs)

    return {
        "total": totals[0],
        "page": page,
        "page_size": page_size,
        "total_tokens": totals[1],
        "prompt_tokens": totals[2],
        "completion_tokens": totals[3],
        "items": [
            {
                "id": l.id,
                "user_id": l.user_id,
                "teacher_name": names["users"].get(l.user_id),
                "teacher_email": names["emails"].get(l.user_id),
                "class_id": l.class_id,
                "class_name": names["classes"].get(l.class_id),
                "prompt_tokens": l.prompt_tokens,
                "completion_tokens": l.completion_tokens,
                "total_tokens": l.total_tokens,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }

