from sqlalchemy import (
    String, Integer, Boolean, ForeignKey, Column, Text, DateTime, Date, Table,
    UniqueConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, date
from db import Base
from utils.time import utcnow

post_enrollments = Table(
    "post_enrollments",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id", ondelete="CASCADE")),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE")),
)

class_members = Table(
    "class_members",
    Base.metadata,
    Column("class_id", Integer, ForeignKey("classes.id", ondelete="CASCADE")),
    Column("user_id",  Integer, ForeignKey("users.id",  ondelete="CASCADE")),
)

# Членство в конкретном потоке (учебном годе) класса. class_members остаётся
# и продолжает наполняться при вступлении (двойная запись) — её читают
# permissions.py и легаси-скрипты; уберём отдельной задачей после переезда.
cohort_students = Table(
    "cohort_students",
    Base.metadata,
    Column("cohort_id", Integer, ForeignKey("cohorts.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("joined_at", DateTime, default=utcnow),
)

chat_members = Table(
    "chat_members",
    Base.metadata,
    Column("chat_id", Integer, ForeignKey("chats.id")),
    Column("user_id", Integer, ForeignKey("users.id")),
)

class Class(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")

    invite_code: Mapped[str] = mapped_column(String(6), unique=True, index=True, nullable=False)

    cover_image: Mapped[str] = mapped_column(Text, nullable=True)
    teacher: Mapped[str] = mapped_column(String(200), nullable=True)
    period: Mapped[str] = mapped_column(String(100), nullable=True)

    # 'manual' — потоки создаются/архивируются только вручную;
    # 'yearly' — класс участвует в ежегодном rollover (POST /rollover).
    rotation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )

    creator: Mapped["User"] = relationship(back_populates="classes_created", foreign_keys=[created_by])
    members: Mapped[list["User"]] = relationship(
        "User",
        secondary=class_members,
        back_populates="classes",
    )
    cohorts: Mapped[list["Cohort"]] = relationship(
        back_populates="klass",
        cascade="all, delete-orphan",
        order_by="Cohort.start_date.desc()",
    )


class Cohort(Base):
    """Поток — конкретный учебный год класса. Класс = вечный шаблон
    (лекции, материалы, задания), поток = набор учеников и дедлайны года."""
    __tablename__ = "cohorts"
    __table_args__ = (
        # У одного класса не более одного активного потока (partial unique
        # index — работает и в Postgres, и в SQLite).
        Index(
            "ux_cohorts_one_active_per_class",
            "class_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    class_id: Mapped[int] = mapped_column(
        ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    academic_year: Mapped[str] = mapped_column(String(16), nullable=False)  # «2026/2027»
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    klass: Mapped["Class"] = relationship(back_populates="cohorts")
    students: Mapped[list["User"]] = relationship(
        "User",
        secondary=cohort_students,
        back_populates="cohorts",
    )
    deadlines: Mapped[list["Deadline"]] = relationship(
        back_populates="cohort",
        cascade="all, delete-orphan",
    )


class Deadline(Base):
    """Дедлайн задания в рамках конкретного потока. Заменяет жёсткое поле
    assignments.deadline: контент общий, а даты у каждого учебного года свои."""
    __tablename__ = "deadlines"
    __table_args__ = (
        UniqueConstraint("cohort_id", "assignment_id", name="ux_deadlines_cohort_assignment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    cohort_id: Mapped[int] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    due_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # После rollover дедлайны создаются черновиками (False) — преподаватель
    # проверяет сдвинутые даты и публикует; ученикам видны только опубликованные.
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    cohort: Mapped["Cohort"] = relationship(back_populates="deadlines")
    assignment: Mapped["Assignment"] = relationship()

class User(Base):
    __tablename__ = "users"
    # Личность = пара (email, org_type): один и тот же email может существовать в
    # университете и школе как разные аккаунты. Глобальный unique на email убран —
    # он конфликтовал с логикой login/register (та ключует по org_type) и ронял
    # регистрацию второго org_type в IntegrityError → 500.
    __table_args__ = (
        UniqueConstraint("email", "org_type", name="ux_users_email_org"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="student", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=True)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    ai_unlimited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    # Версия токенов: инкрементируется при logout/смене пароля. Access/refresh
    # несут её в claim "tv"; при несовпадении токен считается отозванным.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Подтверждён ли email кодом из письма. Существующие аккаунты мигрируются в
    # true (server_default), новые регистрации создаются с false.
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="true")

    posts: Mapped[list["Posts"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    chats: Mapped[list["Chat"]] = relationship(
        "Chat",
        secondary=chat_members,
        back_populates="members",
    )
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="student",
        cascade="all, delete-orphan",
    )
    assignments_created: Mapped[list["Assignment"]] = relationship(
        back_populates="creator",
        cascade="all, delete-orphan",
        foreign_keys="Assignment.created_by",
    )
    classes_created: Mapped[list["Class"]] = relationship(
        back_populates="creator",
        cascade="all, delete-orphan",
        foreign_keys="Class.created_by",
    )
    classes: Mapped[list["Class"]] = relationship(
        "Class",
        secondary=class_members,
        back_populates="members",
    )
    cohorts: Mapped[list["Cohort"]] = relationship(
        "Cohort",
        secondary=cohort_students,
        back_populates="students",
    )

class Posts(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    user: Mapped["User"] = relationship(back_populates="posts")

class Chat(Base):
    __tablename__ = "chats"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    members: Mapped[list["User"]] = relationship(
        "User",
        secondary=chat_members,
        back_populates="chats",
    )

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    content = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    chat_id = Column(Integer, ForeignKey("chats.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    is_read = Column(Boolean, default=False)
    file_url = Column(String, nullable=True)

class Reaction(Base):
    __tablename__ = "reactions"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    emoji = Column(String)

class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    class_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, server_default="0")
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    criteria: Mapped[str] = mapped_column(Text, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, default=100)
    # DEPRECATED: дедлайны переехали в таблицу deadlines (по потокам).
    # Поле оставлено для обратной совместимости и как fallback для заданий
    # с легаси class_id, у которых нет потока. Не удалять до полного переезда.
    deadline: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reference_solution_url: Mapped[str] = mapped_column(String, nullable=True)

    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    creator: Mapped["User"] = relationship(back_populates="assignments_created", foreign_keys=[created_by])

    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
    )
    variants: Mapped[list["AssignmentVariant"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="AssignmentVariant.variant_number",
    )

class AssignmentVariant(Base):
    __tablename__ = "assignment_variants"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=True)
    reference_solution_url: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assignment: Mapped["Assignment"] = relationship(back_populates="variants")

class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # BE-1: одна сдача на пару (задание, студент). Приложение уже проверяет
        # это в student_already_submitted(), но без БД-инварианта двойной клик
        # или «сайт+приложение одновременно» проскакивал проверку (TOCTOU) и
        # создавал две сдачи. Ловим IntegrityError в submit_assignment → 409.
        UniqueConstraint("assignment_id", "student_id", name="ux_submissions_assignment_student"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Дедлайн потока, в рамках которого сдана работа. NULL — легаси-сдачи
    # заданий без потока (битый class_id), для них действует assignment.deadline.
    deadline_id: Mapped[int] = mapped_column(
        ForeignKey("deadlines.id"), nullable=True, index=True
    )

    file_url: Mapped[str] = mapped_column(String, nullable=True)
    file_urls: Mapped[str] = mapped_column(Text, nullable=True)
    text_content: Mapped[str] = mapped_column(Text, nullable=True)
    variant_number: Mapped[int] = mapped_column(Integer, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String, default="submitted")

    assignment: Mapped["Assignment"] = relationship(back_populates="submissions")
    student: Mapped["User"] = relationship(back_populates="submissions")
    grade: Mapped["Grade"] = relationship(
        back_populates="submission",
        uselist=False,
        cascade="all, delete-orphan",
    )

    from typing import ClassVar
    student_name: ClassVar[str] = None

class Grade(Base):
    __tablename__ = "grades"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id"), nullable=False, unique=True
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback: Mapped[str] = mapped_column(Text, nullable=True)
    criteria_scores: Mapped[str] = mapped_column(Text, nullable=True)
    graded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    graded_by: Mapped[str] = mapped_column(String, default="ai")

    submission: Mapped["Submission"] = relationship(back_populates="grade")

EMBED_DIM = 1536

class RagDocument(Base):
    __tablename__ = "rag_documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chunks: Mapped[list["RagChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

class RagChunk(Base):
    __tablename__ = "rag_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("rag_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped["RagDocument"] = relationship(back_populates="chunks")

class AiUsageLog(Base):
    __tablename__ = "ai_usage_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    class_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class AiMessage(Base):
    """Сообщение переписки с ИИ, сохранённое на сервере — чтобы история
    синхронизировалась между приложением и сайтом на всех устройствах.
    Тред = все сообщения одного (user_id, class_id) по порядку id.
    class_id = NULL — глобальный ИИ-экран; иначе — ИИ-вкладка класса."""
    __tablename__ = "ai_messages"
    __table_args__ = (
        Index("ix_ai_messages_thread", "user_id", "class_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[int] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NotificationState(Base):
    """Состояние прочтения/скрытия одного уведомления пользователя. Список
    уведомлений выводится из заданий/оценок (серверные данные), а read/dismissed
    хранятся здесь, чтобы совпадали в приложении и на сайте. notif_key —
    канонический ключ '{kind}:{ref_id}' (kind: assignment|deadline|grade)."""
    __tablename__ = "notification_states"
    __table_args__ = (
        UniqueConstraint("user_id", "notif_key", name="ux_notification_states_user_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notif_key: Mapped[str] = mapped_column(String(64), nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProcessedDocument(Base):
    __tablename__ = "processed_documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    rag_document_id: Mapped[int] = mapped_column(
        ForeignKey("rag_documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)



class TeacherAvatar(Base):

    __tablename__ = "teacher_avatars"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")

    display_name: Mapped[str] = mapped_column(String(200), nullable=True)
    photo_url: Mapped[str] = mapped_column(String, nullable=True)
    voice_sample_url: Mapped[str] = mapped_column(String, nullable=True)

    elevenlabs_voice_id: Mapped[str] = mapped_column(String(128), nullable=True)
    did_presenter_id: Mapped[str] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    voice_clone_warning: Mapped[str] = mapped_column(Text, nullable=True)

    reviewed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    teacher: Mapped["User"] = relationship(foreign_keys=[teacher_id])

    lectures: Mapped[list["AvatarLecture"]] = relationship(
        back_populates="avatar",
        cascade="all, delete-orphan",
    )


class AvatarLecture(Base):

    __tablename__ = "avatar_lectures"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    avatar_id: Mapped[int] = mapped_column(
        ForeignKey("teacher_avatars.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")

    title: Mapped[str] = mapped_column(String(256), nullable=False)

    source_filename: Mapped[str] = mapped_column(String(512), nullable=True)
    source_file_url: Mapped[str] = mapped_column(String, nullable=True)

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    style: Mapped[str] = mapped_column(String(32), nullable=False, default="university")
    # Язык озвучки и конспекта — обучение идёт строго на английском, поэтому
    # дефолт "en"; язык исходных слайдов может быть любым, содержимое переводится.
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en", server_default="en")

    auto_summary: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_approval")
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    reviewed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    summary_text: Mapped[str] = mapped_column(Text, nullable=True)
    intro_video_url: Mapped[str] = mapped_column(String, nullable=True)

    estimated_chars: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    avatar: Mapped["TeacherAvatar"] = relationship(back_populates="lectures")
    slides: Mapped[list["AvatarLectureSlide"]] = relationship(
        back_populates="lecture",
        cascade="all, delete-orphan",
        order_by="AvatarLectureSlide.slide_index",
    )


class AvatarLectureSlide(Base):
    """Один слайд лекции: картинка слайда + текст рассказа аватара + готовое аудио."""
    __tablename__ = "avatar_lecture_slides"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    lecture_id: Mapped[int] = mapped_column(
        ForeignKey("avatar_lectures.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slide_index: Mapped[int] = mapped_column(Integer, nullable=False)

    slide_image_url: Mapped[str] = mapped_column(String, nullable=True)
    slide_source_text: Mapped[str] = mapped_column(Text, nullable=True)

    narration_text: Mapped[str] = mapped_column(Text, nullable=True)
    audio_url: Mapped[str] = mapped_column(String, nullable=True)
    audio_duration_seconds: Mapped[float] = mapped_column(Integer, nullable=True)

    lecture: Mapped["AvatarLecture"] = relationship(back_populates="slides")

class EmailCode(Base):
    """Одноразовый 6-значный код для подтверждения email и сброса пароля.
    Код хранится только в виде хэша. На пару (email, org_type, purpose) держим
    не более одной активной записи — новый запрос затирает предыдущий."""
    __tablename__ = "email_codes"
    __table_args__ = (
        Index("ix_email_codes_lookup", "email", "org_type", "purpose"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # 'verify' | 'reset'
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DeviceToken(Base):
    """FCM-токен устройства пользователя для push-уведомлений. У одного юзера
    может быть несколько устройств. token уникален глобально: если то же
    устройство залогинилось под другим аккаунтом — запись переезжает на нового
    владельца (upsert по token в /push/register)."""
    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="ux_device_tokens_token"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=True)  # android | ios
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class PushLog(Base):
    """Дедуп однократных пушей (напр. напоминание о дедлайне): чтобы фоновый
    цикл не слал одно и то же уведомление повторно на каждой итерации и после
    рестарта. dedup_key — произвольный ключ события, напр. 'deadline:{id}'."""
    __tablename__ = "push_log"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_key", name="ux_push_log_user_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
