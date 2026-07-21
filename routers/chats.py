from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from db import get_db
from deps import get_current_user
from models import Chat, chat_members, Message
from permissions import require_chat_member
from schemas import ChatCreate, ChatResponse, LastMessagePreview
from models import User

router = APIRouter(prefix="/chats", tags=["Chats"])

@router.post("/", response_model=ChatResponse)
def create_chat(chat: ChatCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    db_chat = Chat(name=chat.name)
    db.add(db_chat)
    db.commit()
    db.refresh(db_chat)
    db.execute(chat_members.insert().values(chat_id=db_chat.id, user_id=current_user.id))
    db.commit()
    return db_chat

@router.get("/", response_model=list[ChatResponse])
def get_chats(
    limit: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    q = (
        db.query(Chat)
        .join(chat_members)
        .filter(chat_members.c.user_id == current_user.id)
        .order_by(Chat.id.desc())
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    chats = q.all()

    chat_ids = [c.id for c in chats]
    unread_map: dict[int, int] = {}
    last_map: dict[int, Message] = {}
    if chat_ids:
        rows = (
            db.query(Message.chat_id, func.count(Message.id))
            .filter(Message.chat_id.in_(chat_ids))
            .filter(Message.user_id != current_user.id)
            .filter(or_(Message.is_read.is_(False), Message.is_read.is_(None)))
            .group_by(Message.chat_id)
            .all()
        )
        unread_map = {cid: cnt for cid, cnt in rows}

        # Последнее сообщение на чат — превью в списке без загрузки полной
        # истории (раньше клиент тянул все сообщения каждого чата на поллинге).
        latest_sub = (
            db.query(Message.chat_id, func.max(Message.id).label("max_id"))
            .filter(Message.chat_id.in_(chat_ids))
            .group_by(Message.chat_id)
            .subquery()
        )
        last_rows = (
            db.query(Message)
            .join(latest_sub, (Message.chat_id == latest_sub.c.chat_id) & (Message.id == latest_sub.c.max_id))
            .all()
        )
        last_map = {m.chat_id: m for m in last_rows}

    def _preview(c: Chat) -> LastMessagePreview | None:
        m = last_map.get(c.id)
        if m is None:
            return None
        return LastMessagePreview(
            id=m.id,
            content=m.content or "",
            user_id=m.user_id,
            created_at=m.created_at.isoformat() if m.created_at else None,
            is_read=bool(m.is_read),
        )

    return [
        ChatResponse(id=c.id, name=c.name, unread_count=unread_map.get(c.id, 0), last_message=_preview(c))
        for c in chats
    ]


@router.put("/{chat_id}/read")
def mark_chat_read(chat_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_chat_member(db, chat_id, current_user.id)
    db.query(Message).filter(
        Message.chat_id == chat_id,
        Message.user_id != current_user.id,
        or_(Message.is_read.is_(False), Message.is_read.is_(None)),
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return {"status": "read"}

@router.get("/{chat_id}/users")
def get_chat_users(chat_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_chat_member(db, chat_id, current_user.id)
    users = (
        db.query(User)
        .join(chat_members)
        .filter(chat_members.c.chat_id == chat_id)
        .all()
    )
    return [{"id": u.id, "email": u.email, "role": u.role, "is_active": u.is_active} for u in users]

@router.post("/{chat_id}/users/{user_id}")
def add_user_to_chat(chat_id: int, user_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_chat_member(db, chat_id, current_user.id)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.org_type != current_user.org_type:
        raise HTTPException(status_code=403, detail="Нельзя добавить пользователя из другой организации")
    existing = db.execute(
        chat_members.select().where(
            chat_members.c.chat_id == chat_id,
            chat_members.c.user_id == user_id
        )
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="User already in chat")
    db.execute(chat_members.insert().values(chat_id=chat_id, user_id=user_id))
    db.commit()
    return {"message": "User added"}

@router.delete("/{chat_id}")
def delete_chat(chat_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_chat_member(db, chat_id, current_user.id)
    chat = db.query(Chat).filter(Chat.id == chat_id).first()
    # Delete messages, members, then the chat
    db.query(Message).filter(Message.chat_id == chat_id).delete()
    db.execute(chat_members.delete().where(chat_members.c.chat_id == chat_id))
    db.delete(chat)
    db.commit()
    return {"message": "Chat deleted"}

@router.delete("/{chat_id}/users/{user_id}")
def remove_user_from_chat(chat_id: int, user_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_chat_member(db, chat_id, current_user.id)
    db.execute(
        chat_members.delete().where(
            chat_members.c.chat_id == chat_id,
            chat_members.c.user_id == user_id
        )
    )
    db.commit()
    return {"message": "User removed"}
