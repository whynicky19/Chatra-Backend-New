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

def _last_activity_map(db: Session, user_ids) -> dict:
    """Последнее действие пользователя.

    У аккаунта нет last_seen (сессии не пишутся), поэтому берём максимум по
    трём следам, которые остаются в БД: запрос к ИИ, опубликованный пост,
    сданная работа. Это «последнее действие», а не «был онлайн» — так это и
    подписано в интерфейсе.
    """
    from sqlalchemy import func

    from models import Posts, Submission

    ids = list(user_ids)
    if not ids:
        return {}
    out: dict = {}

    def _merge(rows):
        for uid, ts in rows:
            if ts and (uid not in out or ts > out[uid]):
                out[uid] = ts

    _merge(db.query(AiUsageLog.user_id, func.max(AiUsageLog.created_at))
           .filter(AiUsageLog.user_id.in_(ids)).group_by(AiUsageLog.user_id).all())
    _merge(db.query(Posts.user_id, func.max(Posts.created_at))
           .filter(Posts.user_id.in_(ids)).group_by(Posts.user_id).all())
    _merge(db.query(Submission.student_id, func.max(Submission.submitted_at))
           .filter(Submission.student_id.in_(ids)).group_by(Submission.student_id).all())
    return out


def _ai_by_endpoint_rows(db: Session, *where) -> list:
    """Расход по видам запросов для одного среза (пользователь / класс)."""
    from sqlalchemy import desc, func

    rows = (db.query(AiUsageLog.endpoint,
                     func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                     func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                     func.coalesce(func.sum(AiUsageLog.completion_tokens), 0),
                     func.count(AiUsageLog.id))
            .filter(*where)
            .group_by(AiUsageLog.endpoint)
            .order_by(desc(func.sum(AiUsageLog.total_tokens)))
            .all())
    out = []
    for endpoint, total, prompt, completion, requests in rows:
        group = AI_ENDPOINT_GROUPS.get(endpoint, "other")
        out.append({
            "endpoint": endpoint,
            "label": AI_ENDPOINT_LABELS.get(endpoint, endpoint),
            "group": group,
            "group_label": AI_GROUP_LABELS[group],
            "total_tokens": int(total),
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "request_count": int(requests),
        })
    return out


def _ai_totals(db: Session, *where) -> dict:
    from sqlalchemy import func

    r = (db.query(func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                  func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                  func.coalesce(func.sum(AiUsageLog.completion_tokens), 0),
                  func.count(AiUsageLog.id),
                  func.min(AiUsageLog.created_at),
                  func.max(AiUsageLog.created_at))
         .filter(*where).one())
    requests = int(r[3])
    return {
        "total_tokens": int(r[0]),
        "prompt_tokens": int(r[1]),
        "completion_tokens": int(r[2]),
        "request_count": requests,
        "avg_tokens": round(int(r[0]) / requests) if requests else 0,
        "first_used": r[4].isoformat() if r[4] else None,
        "last_used": r[5].isoformat() if r[5] else None,
    }


# ВАЖНО: объявлено до "/users/{user_id}" — иначе FastAPI попробует разобрать
# "overview" как int и вернёт 422.
@router.get("/users/overview")
def get_users_overview(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Список пользователей с агрегатами для админки.

    Отдельно от `GET /users` (тот отдаёт чистый UserResponse и его читают
    другие клиенты): здесь к каждому аккаунту сразу приложены расход токенов,
    число классов и последнее действие. Иначе список на 50 человек означал бы
    150 дополнительных запросов из браузера.
    """
    from sqlalchemy import func

    from models import Class, class_members

    org = current_user.org_type
    users = db.query(User).filter(User.org_type == org).order_by(User.id).all()
    ids = [u.id for u in users]

    ai: dict = {}
    classes_count: dict = {}
    if ids:
        for uid, tokens, requests in (
            db.query(AiUsageLog.user_id,
                     func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                     func.count(AiUsageLog.id))
            .filter(AiUsageLog.org_type == org, AiUsageLog.user_id.in_(ids))
            .group_by(AiUsageLog.user_id).all()
        ):
            ai[uid] = (int(tokens), int(requests))
        # Через join с classes: членство само по себе не знает про org_type.
        for uid, cnt in (
            db.query(class_members.c.user_id, func.count(func.distinct(class_members.c.class_id)))
            .select_from(class_members)
            .join(Class, Class.id == class_members.c.class_id)
            .filter(Class.org_type == org, class_members.c.user_id.in_(ids))
            .group_by(class_members.c.user_id).all()
        ):
            classes_count[uid] = int(cnt)
    last = _last_activity_map(db, ids)

    return [
        {
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": u.is_active,
            "is_verified": u.is_verified,
            "ai_unlimited": u.ai_unlimited,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "total_tokens": ai.get(u.id, (0, 0))[0],
            "request_count": ai.get(u.id, (0, 0))[1],
            "class_count": classes_count.get(u.id, 0),
            "last_active": last[u.id].isoformat() if u.id in last else None,
        }
        for u in users
    ]


@router.get("/users/{user_id}")
def get_user_detail(
    user_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Карточка пользователя: профиль, расход ИИ, классы и учебная активность.

    Одним запросом — чтобы модалка открывалась сразу целиком, а не догружала
    четыре блока по очереди.
    """
    from sqlalchemy import func

    from models import Assignment, Class, Grade, Posts, Submission, class_members
    from services import ai_quota

    user = db.query(User).filter(User.id == user_id).first()
    if not user or user.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="User not found")

    org = current_user.org_type
    mine = (AiUsageLog.org_type == org, AiUsageLog.user_id == user_id)

    # ── Расход по классам этого пользователя ─────────────────────────────────
    per_class: dict = {}
    for class_id, tokens, requests in (
        db.query(AiUsageLog.class_id,
                 func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                 func.count(AiUsageLog.id))
        .filter(*mine).group_by(AiUsageLog.class_id).all()
    ):
        per_class[class_id] = (int(tokens), int(requests))

    # ── Классы: где состоит + что создал ─────────────────────────────────────
    member_ids = {r[0] for r in db.query(class_members.c.class_id)
                  .filter(class_members.c.user_id == user_id).all()}
    created_ids = {r[0] for r in db.query(Class.id)
                   .filter(Class.created_by == user_id).all()}
    classes = []
    if member_ids | created_ids:
        for cl in (db.query(Class)
                   .filter(Class.id.in_(member_ids | created_ids), Class.org_type == org)
                   .order_by(Class.name).all()):
            tokens, requests = per_class.get(cl.id, (0, 0))
            classes.append({
                "id": cl.id,
                "name": cl.name,
                "role": "creator" if cl.id in created_ids else "member",
                "cover_color": cl.cover_color,
                "cover_icon": cl.cover_icon,
                "cover_thumbnail": cl.cover_thumbnail,
                "total_tokens": tokens,
                "request_count": requests,
            })
    # Расход вне предметов (главный ассистент) — отдельной строкой, иначе он
    # просто исчезает из карточки.
    general_tokens, general_requests = per_class.get(None, (0, 0))

    # ── Учебная активность ───────────────────────────────────────────────────
    posts_count = db.query(func.count(Posts.id)).filter(Posts.user_id == user_id).scalar() or 0
    submissions_count = (db.query(func.count(Submission.id))
                         .filter(Submission.student_id == user_id).scalar() or 0)
    graded = (db.query(func.count(Grade.id), func.avg(Grade.score))
              .select_from(Grade)
              .join(Submission, Submission.id == Grade.submission_id)
              .filter(Submission.student_id == user_id).one())
    assignments_created = (db.query(func.count(Assignment.id))
                           .filter(Assignment.created_by == user_id).scalar() or 0)

    last = _last_activity_map(db, [user_id])

    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "ai_unlimited": user.ai_unlimited,
        "org_type": user.org_type,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_active": last[user_id].isoformat() if user_id in last else None,
        "ai": {
            **_ai_totals(db, *mine),
            "by_endpoint": _ai_by_endpoint_rows(db, *mine),
            # Дневная квота сообщений — то, по чему бэкенд реально отказывает в
            # запросе (services/ai_quota.py). Токены к ней отношения не имеют.
            "messages_today": ai_quota.messages_used_today(db, user_id),
            "message_limit": ai_quota.daily_message_limit(),
            "general_tokens": general_tokens,
            "general_requests": general_requests,
        },
        "classes": classes,
        "activity": {
            "posts": int(posts_count),
            "submissions": int(submissions_count),
            "graded": int(graded[0] or 0),
            "avg_score": round(float(graded[1]), 1) if graded[1] is not None else None,
            "assignments_created": int(assignments_created),
        },
    }


@router.get("/classes/{class_id}")
def get_class_detail(
    class_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Карточка предмета: состав, потоки, содержимое и расход ИИ."""
    from sqlalchemy import func

    from models import Assignment, Cohort, Grade, Posts, Submission, cohort_students

    obj = crud_classes.get_class(db, class_id)
    if not obj or obj.org_type != current_user.org_type:
        raise HTTPException(status_code=404, detail="Not found")

    org = current_user.org_type
    scope = (AiUsageLog.org_type == org, AiUsageLog.class_id == class_id)

    # ── Участники с их расходом внутри этого предмета ────────────────────────
    members = crud_classes.get_members(db, class_id)
    member_ids = [m.id for m in members]
    per_user: dict = {}
    for uid, tokens, requests in (
        db.query(AiUsageLog.user_id,
                 func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                 func.count(AiUsageLog.id))
        .filter(*scope).group_by(AiUsageLog.user_id).all()
    ):
        per_user[uid] = (int(tokens), int(requests))
    last_seen = _last_activity_map(db, member_ids)
    member_rows = sorted(
        [{
            "id": m.id,
            "full_name": m.full_name,
            "email": m.email,
            "role": m.role,
            "is_active": m.is_active,
            "total_tokens": per_user.get(m.id, (0, 0))[0],
            "request_count": per_user.get(m.id, (0, 0))[1],
            "last_active": last_seen[m.id].isoformat() if m.id in last_seen else None,
        } for m in members],
        key=lambda r: -r["total_tokens"],
    )

    # ── Потоки (учебные годы) ────────────────────────────────────────────────
    cohorts = []
    for c in (db.query(Cohort).filter(Cohort.class_id == class_id)
              .order_by(Cohort.academic_year.desc()).all()):
        students = (db.query(func.count(cohort_students.c.student_id))
                    .filter(cohort_students.c.cohort_id == c.id).scalar() or 0)
        cohorts.append({
            "id": c.id,
            "academic_year": c.academic_year,
            "status": c.status,
            "student_count": int(students),
            "start_date": c.start_date.isoformat() if c.start_date else None,
        })

    # ── Содержимое ───────────────────────────────────────────────────────────
    assignments_total = (db.query(func.count(Assignment.id))
                         .filter(Assignment.class_id == class_id).scalar() or 0)
    assignments_active = (db.query(func.count(Assignment.id))
                          .filter(Assignment.class_id == class_id,
                                  Assignment.is_active.is_(True)).scalar() or 0)
    # Лекции связаны с классом префиксом заголовка (crud/posts.py), отдельной
    # колонки posts.class_id в схеме нет.
    lectures = (db.query(func.count(Posts.id))
                .filter(Posts.title.like(f"[LECTURE][{class_id}]%")).scalar() or 0)
    submissions = (db.query(func.count(Submission.id))
                   .select_from(Submission)
                   .join(Assignment, Assignment.id == Submission.assignment_id)
                   .filter(Assignment.class_id == class_id).scalar() or 0)
    graded = (db.query(func.count(Grade.id), func.avg(Grade.score))
              .select_from(Grade)
              .join(Submission, Submission.id == Grade.submission_id)
              .join(Assignment, Assignment.id == Submission.assignment_id)
              .filter(Assignment.class_id == class_id).one())

    creator = db.query(User).filter(User.id == obj.created_by).first()

    return {
        "id": obj.id,
        "name": obj.name,
        "description": obj.description,
        "invite_code": obj.invite_code,
        "created_at": obj.created_at.isoformat() if obj.created_at else None,
        "cover_image": obj.cover_image,
        "cover_thumbnail": obj.cover_thumbnail,
        "cover_color": obj.cover_color,
        "cover_icon": obj.cover_icon,
        "teacher": obj.teacher,
        "creator": {
            "id": creator.id, "full_name": creator.full_name,
            "email": creator.email, "role": creator.role,
        } if creator else None,
        "members": member_rows,
        "member_count": len(member_rows),
        "cohorts": cohorts,
        "content": {
            "assignments": int(assignments_total),
            "assignments_active": int(assignments_active),
            "lectures": int(lectures),
            "submissions": int(submissions),
            "graded": int(graded[0] or 0),
            "avg_score": round(float(graded[1]), 1) if graded[1] is not None else None,
        },
        "ai": {
            **_ai_totals(db, *scope),
            "by_endpoint": _ai_by_endpoint_rows(db, *scope),
        },
    }


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
    class_id: Optional[int] = Query(None, description="Filter by class; None = all, 0 = usage outside any class"),
    endpoint: Optional[str] = Query(None, description="Filter by kind(s) of request, comma-separated"),
    user_id: Optional[int] = Query(None, description="Filter by user"),
    days: Optional[int] = Query(None, ge=1, le=365, description="Only the last N days (UTC), None = all time"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Журнал расхода токенов.

    Фильтры endpoint/user_id/days добавлены, чтобы дашборд был кликабельным:
    выбрав вид расхода («обложки») или пользователя на диаграмме, админ видит
    ровно те строки, из которых сложилась цифра, а не весь журнал подряд.
    """
    from sqlalchemy import desc
    q = db.query(AiUsageLog).filter(AiUsageLog.org_type == current_user.org_type)
    if class_id == 0:
        # Расход вне предмета (общий чат) хранится как class_id IS NULL, а
        # class_id=None в параметрах уже занят значением «все классы» — поэтому
        # для него отдельное значение 0, иначе эту строку дашборда нельзя было
        # бы раскрыть в журнал.
        q = q.filter(AiUsageLog.class_id.is_(None))
    elif class_id is not None:
        q = q.filter(AiUsageLog.class_id == class_id)
    if endpoint:
        # Список через запятую: одна функция продукта бывает несколькими
        # endpoint'ами (чат — это chat + chat_vision), и клик по её строке в
        # дашборде должен открывать журнал целиком, а не одну её половину.
        kinds = [e.strip() for e in endpoint.split(",") if e.strip()]
        if kinds:
            q = q.filter(AiUsageLog.endpoint.in_(kinds))
    if user_id is not None:
        q = q.filter(AiUsageLog.user_id == user_id)
    if days is not None:
        q = q.filter(AiUsageLog.created_at >= _period_start(days))
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
# (cover_image) и ИИ-проверка работ (ai-grade / ai-grade-auto из
# services/deadline_checker.py).
AI_ENDPOINT_LABELS = {
    "chat": "Чат с ИИ",
    "chat_vision": "Чат с ИИ (с изображением)",
    "ai_chat": "Чат с ИИ (старые записи)",
    "ai_title": "Название чата",
    "cover_image": "Обложка предмета",
    "ai-grade": "Проверка работ",
    "ai-grade-auto": "Проверка работ (по дедлайну)",
}

# Семейство расхода: несколько endpoint'ов отвечают за одну функцию продукта
# (чат — это chat + chat_vision + старый ai_chat, проверка — ручная и по
# дедлайну). Дашборд группирует по нему, чтобы «сколько стоит чат» читалось
# одним числом, но детализация по endpoint при этом не терялась.
AI_ENDPOINT_GROUPS = {
    "chat": "chat",
    "chat_vision": "chat",
    "ai_chat": "chat",
    "ai_title": "title",
    "cover_image": "cover",
    "ai-grade": "grade",
    "ai-grade-auto": "grade",
}
AI_GROUP_LABELS = {
    "chat": "Чат с ИИ",
    "title": "Названия чатов",
    "cover": "Обложки предметов",
    "grade": "Проверка работ",
    "other": "Прочее",
}


def _period_start(days: int):
    """Начало периода: полночь UTC того дня, с которого считаем.

    Считаем по календарным суткам UTC (created_at хранится naive-UTC), а не
    «минус 24*N часов от сейчас»: иначе на графике по дням первый столбик
    всегда оказывался бы обрезанным.
    """
    from datetime import timedelta

    from utils.time import utcnow

    now = utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)


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


@router.get("/ai-usage/dashboard")
def get_ai_usage_dashboard(
    days: int = Query(30, ge=1, le=365, description="Окно отчёта в календарных сутках UTC"),
    top_users: int = Query(10, ge=1, le=50),
    top_classes: int = Query(12, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_admin),
):
    """Полная картина расхода токенов одним запросом.

    Раньше админка собирала дашборд из трёх ручек и всё равно не отвечала на
    вопросы «когда именно потратили», «кто потратил» и «какая функция сколько
    стоит внутри конкретного класса». Здесь один срез: итоги за период и за всё
    время, разбивка по видам запросов (чат / названия чатов / обложки /
    проверка работ), по классам, по дням и по пользователям — каждая с
    prompt/completion, числом запросов и средним чеком запроса.

    Сутки считаются в UTC — так же, как их считают дневная квота сообщений
    (services/ai_quota.py) и дневной бюджет организации (services/ai_budget.py),
    иначе цифра «за сегодня» в дашборде расходилась бы с той, по которой
    бэкенд реально отказывает в запросе.
    """
    from datetime import timedelta

    from sqlalchemy import desc, func

    from models import Class
    from services import ai_budget, ai_quota
    from utils.time import utcnow

    org = current_user.org_type
    since = _period_start(days)
    period = (AiUsageLog.org_type == org, AiUsageLog.created_at >= since)
    all_time = (AiUsageLog.org_type == org,)

    def _totals(*where):
        r = (db.query(
                func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.completion_tokens), 0),
                func.count(AiUsageLog.id),
                func.count(func.distinct(AiUsageLog.user_id)),
                func.count(func.distinct(AiUsageLog.class_id)),
             ).filter(*where).one())
        requests = int(r[3])
        return {
            "total_tokens": int(r[0]),
            "prompt_tokens": int(r[1]),
            "completion_tokens": int(r[2]),
            "request_count": requests,
            # COUNT(DISTINCT) не считает NULL: класс NULL — это общий чат, а не
            # ещё один класс; пользователь NULL — расход удалённого аккаунта.
            "user_count": int(r[4]),
            "class_count": int(r[5]),
            "avg_tokens": round(int(r[0]) / requests) if requests else 0,
        }

    def _breakdown(column, *where, limit=None):
        """Расход в разрезе одной колонки: (ключ, суммы) по убыванию токенов."""
        q = (db.query(
                column,
                func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.completion_tokens), 0),
                func.count(AiUsageLog.id),
                func.count(func.distinct(AiUsageLog.user_id)),
                func.max(AiUsageLog.created_at),
             )
             .filter(*where)
             .group_by(column)
             .order_by(desc(func.sum(AiUsageLog.total_tokens))))
        if limit:
            q = q.limit(limit)
        out = []
        for r in q.all():
            requests = int(r[4])
            out.append({
                "key": r[0],
                "total_tokens": int(r[1]),
                "prompt_tokens": int(r[2]),
                "completion_tokens": int(r[3]),
                "request_count": requests,
                "user_count": int(r[5]),
                "avg_tokens": round(int(r[1]) / requests) if requests else 0,
                "last_used": r[6].isoformat() if r[6] else None,
            })
        return out

    totals = _totals(*period)
    totals_all = _totals(*all_time)

    # ── Виды запросов: на что именно ушли токены ─────────────────────────────
    by_endpoint = []
    for row in _breakdown(AiUsageLog.endpoint, *period):
        endpoint = row.pop("key")
        group = AI_ENDPOINT_GROUPS.get(endpoint, "other")
        by_endpoint.append({
            "endpoint": endpoint,
            # Незнакомый endpoint (новая функция, старые записи) показываем как
            # есть — иначе часть расхода молча выпала бы из отчёта.
            "label": AI_ENDPOINT_LABELS.get(endpoint, endpoint),
            "group": group,
            "group_label": AI_GROUP_LABELS[group],
            **row,
        })

    # Свёртка по семействам функций: chat + chat_vision — это одна строка «Чат».
    groups: dict[str, dict] = {}
    for e in by_endpoint:
        g = groups.setdefault(e["group"], {
            "group": e["group"], "label": e["group_label"],
            "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "request_count": 0, "endpoints": [],
        })
        for k in ("total_tokens", "prompt_tokens", "completion_tokens", "request_count"):
            g[k] += e[k]
        g["endpoints"].append(e["endpoint"])
    by_group = sorted(groups.values(), key=lambda g: -g["total_tokens"])

    # ── По классам ───────────────────────────────────────────────────────────
    class_rows = _breakdown(AiUsageLog.class_id, *period, limit=top_classes)
    class_ids = [r["key"] for r in class_rows if r["key"]]
    class_names = dict(db.query(Class.id, Class.name)
                       .filter(Class.id.in_(class_ids)).all()) if class_ids else {}
    # Разрез «класс × вид запроса»: видно, что в одном предмете дорогая
    # переписка, а в другом — генерация обложек.
    class_kinds: dict = {}
    if class_rows:
        keys = [r["key"] for r in class_rows]
        q = (db.query(AiUsageLog.class_id, AiUsageLog.endpoint,
                      func.coalesce(func.sum(AiUsageLog.total_tokens), 0))
             .filter(*period)
             .group_by(AiUsageLog.class_id, AiUsageLog.endpoint))
        for cid, endpoint, tokens in q.all():
            if cid in keys:
                class_kinds.setdefault(cid, {})[endpoint] = int(tokens)
    by_class = [{
        "class_id": r["key"],
        # None — класс удалён, а расход остался: строку не прячем, иначе итог
        # перестанет сходиться с суммой сверху.
        "class_name": class_names.get(r["key"]) if r["key"] else None,
        "kinds": class_kinds.get(r["key"], {}),
        **{k: v for k, v in r.items() if k != "key"},
    } for r in class_rows]

    # ── По дням: динамика расхода, с разбивкой по видам ──────────────────────
    day_col = func.date(AiUsageLog.created_at)
    day_rows = (db.query(day_col, AiUsageLog.endpoint,
                         func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                         func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
                         func.coalesce(func.sum(AiUsageLog.completion_tokens), 0),
                         func.count(AiUsageLog.id))
                .filter(*period)
                .group_by(day_col, AiUsageLog.endpoint)
                .all())
    # func.date() отдаёт строку в SQLite и date в Postgres — приводим к
    # 'YYYY-MM-DD', иначе ключи дней не совпали бы с сеткой ниже.
    per_day: dict[str, dict] = {}
    for day, endpoint, total, prompt, completion, requests in day_rows:
        key = str(day)[:10]
        d = per_day.setdefault(key, {"date": key, "total_tokens": 0, "prompt_tokens": 0,
                                     "completion_tokens": 0, "request_count": 0, "kinds": {}})
        d["total_tokens"] += int(total)
        d["prompt_tokens"] += int(prompt)
        d["completion_tokens"] += int(completion)
        d["request_count"] += int(requests)
        d["kinds"][endpoint] = d["kinds"].get(endpoint, 0) + int(total)
    # Пустые дни отдаём нулями: иначе график сжимал бы паузы и показывал
    # ровную линию там, где ИИ вообще не пользовались.
    by_day = []
    for i in range(days):
        key = (since + timedelta(days=i)).date().isoformat()
        by_day.append(per_day.get(key, {"date": key, "total_tokens": 0, "prompt_tokens": 0,
                                        "completion_tokens": 0, "request_count": 0, "kinds": {}}))

    # ── Топ пользователей ────────────────────────────────────────────────────
    user_rows = _breakdown(AiUsageLog.user_id, *period,
                           AiUsageLog.user_id.isnot(None), limit=top_users)
    user_ids = [r["key"] for r in user_rows]
    people = {u.id: u for u in db.query(User.id, User.full_name, User.email, User.role,
                                        User.ai_unlimited)
              .filter(User.id.in_(user_ids)).all()} if user_ids else {}
    user_kinds: dict = {}
    if user_ids:
        q = (db.query(AiUsageLog.user_id, AiUsageLog.endpoint,
                      func.coalesce(func.sum(AiUsageLog.total_tokens), 0))
             .filter(*period, AiUsageLog.user_id.in_(user_ids))
             .group_by(AiUsageLog.user_id, AiUsageLog.endpoint))
        for uid, endpoint, tokens in q.all():
            user_kinds.setdefault(uid, {})[endpoint] = int(tokens)
    top = []
    for r in user_rows:
        u = people.get(r["key"])
        top.append({
            "user_id": r["key"],
            "name": u.full_name if u else None,
            "email": u.email if u else None,
            "role": u.role if u else None,
            "ai_unlimited": bool(u.ai_unlimited) if u else False,
            "kinds": user_kinds.get(r["key"], {}),
            **{k: v for k, v in r.items() if k not in ("key", "user_count")},
        })

    return {
        "days": days,
        "since": since.isoformat(),
        "generated_at": utcnow().isoformat(),
        "totals": totals,
        "totals_all_time": totals_all,
        "by_endpoint": by_endpoint,
        "by_group": by_group,
        "by_class": by_class,
        "by_day": by_day,
        "top_users": top,
        "labels": AI_ENDPOINT_LABELS,
        # Лимиты, на фоне которых читается расход: дневной бюджет токенов
        # организации и дневной лимит сообщений на пользователя.
        "limits": {
            "daily_token_budget": ai_budget.daily_budget(),
            "tokens_used_today": ai_budget.tokens_used_today(db, org),
            "daily_message_limit": ai_quota.daily_message_limit(),
        },
    }


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

