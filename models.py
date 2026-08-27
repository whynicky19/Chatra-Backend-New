from sqlalchemy import (
    String, Integer, Boolean, ForeignKey, Column, Text, DateTime, Date, Table,
    UniqueConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime, date
from typing import Optional
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
    # Уменьшенная версия обложки (≤480px) для карточек списка классов —
    # см. services/image_processing.py. Может быть NULL для обложек,
    # созданных до появления миниатюр.
    cover_thumbnail: Mapped[str] = mapped_column(Text, nullable=True)

    # Оформление обложки: слаг цвета из палитры и слаг предметной иконки
    # (services/cover_art.py: PALETTE / ICONS). По ним обложка генерируется
    # заново («Перегенерировать») и рисуется локальный фолбэк, поэтому они
    # хранятся отдельно от самой картинки.
    #
    # NULL в cover_color/cover_icon = класс ещё на старой системе, где
    # преподаватель загружал свою фотографию. Такие классы продолжают
    # показывать своё изображение как есть; на новую систему класс
    # переезжает в момент, когда преподаватель впервые сгенерирует обложку
    # (см. routers/classes.py: generate_cover). Массовой миграции нет
    # намеренно — старые картинки ничем не хуже и удалять их не за что.
    cover_color: Mapped[str] = mapped_column(String(16), nullable=True)
    cover_icon: Mapped[str] = mapped_column(String(32), nullable=True)
    # 'ai' | 'fallback' | 'upload' (см. services/cover_generator.py). Нужен,
    # чтобы UI мог сказать «модель была недоступна, это запасной вариант»
    # и предложить повторить, не гадая по самой картинке.
    cover_source: Mapped[str] = mapped_column(String(16), nullable=True)

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
    # Личность = пара (email, org_type): один email живёт в вузе и школе как разные
    # аккаунты. Глобальный unique на email ронял регистрацию второго org_type в 500.
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
    # Дата регистрации. NULL у аккаунтов, созданных до migrations/022: настоящей
    # даты для них нет, и админка честно показывает «—» вместо даты миграции.
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=utcnow, nullable=True)

    posts: Mapped[list["Posts"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
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
    # Порядковый номер лекции внутри класса (1, 2, 3...), проставляется при
    # создании поста-лекции — см. crud/posts.py:_next_lecture_position. NULL
    # для не-лекционных постов и для лекций, созданных до migrations/017.
    position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    class_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, server_default="0")
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    criteria: Mapped[str] = mapped_column(Text, nullable=False)
    max_score: Mapped[int] = mapped_column(Integer, default=100)
    # DEPRECATED: дедлайны переехали в deadlines (по потокам). Оставлено как
    # fallback для легаси-заданий без потока — не удалять до полного переезда.
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

class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # Одна сдача на (задание, студент). Проверка в коде есть, но без инварианта
        # БД двойной клик проскакивал её по TOCTOU. IntegrityError → 409 (BE-1).
        UniqueConstraint("assignment_id", "student_id", name="ux_submissions_assignment_student"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), nullable=False)
    # Индекс нужен для выборок по студенту: /assignments/student/my-submissions,
    # my-rating и join рейтинга фильтруют по student_id в одиночку. Составной
    # unique (assignment_id, student_id) ведёт по assignment_id и такие запросы
    # не покрывает — без этого индекса шёл seq scan submissions.
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # Дедлайн потока, в рамках которого сдана работа. NULL — легаси-сдачи
    # заданий без потока (битый class_id), для них действует assignment.deadline.
    deadline_id: Mapped[int] = mapped_column(
        ForeignKey("deadlines.id"), nullable=True, index=True
    )

    file_url: Mapped[str] = mapped_column(String, nullable=True)
    file_urls: Mapped[str] = mapped_column(Text, nullable=True)
    text_content: Mapped[str] = mapped_column(Text, nullable=True)
    # LEGACY: фича "варианты задания" убрана целиком — колонка остаётся
    # только чтобы не терять исторические данные старых сдач, новые всегда NULL.
    variant_number: Mapped[int] = mapped_column(Integer, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # "submitted" | "grading" | "graded" | "late" | "needs_review" (низкая
    # уверенность ИИ в оценке — публикация задержана, ждёт ручной проверки
    # учителем).
    status: Mapped[str] = mapped_column(String, default="submitted")

    # Уверенность ИИ в оценке (0-100) и причины — считаются для любого типа
    # сдачи (текст/документ/фото), см. services/ai_grader.py::_parse_confidence.
    # NULL, пока сдачу ни разу не проверял ИИ.
    ai_confidence: Mapped[int] = mapped_column(Integer, nullable=True)
    ai_review_reasons: Mapped[str] = mapped_column(Text, nullable=True)  # JSON-список строк

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
    """Один источник материала лекции (текст самой лекции ИЛИ один
    прикреплённый файл) — единица инжеста в RAG-конвейер класса
    (services/rag_ingest.py). Раньше эта таблица существовала только в
    schema.py и нигде не заполнялась — репетитор класса работал через
    client-side lecture_context (весь текст лекций целиком в промпте).
    Теперь это реальный источник для векторного поиска (services/rag_search.py)."""
    __tablename__ = "rag_documents"
    __table_args__ = (
        # Идемпотентность инжеста: повторный запуск на тот же файл/тот же
        # "текст лекции" не плодит дублей — апдейтит существующую строку.
        # file_url синтетический ("lecture-body:{post_id}") для текста самой
        # лекции, чтобы колонка была NOT NULL и участвовала в уникальности.
        UniqueConstraint("post_id", "file_url", name="ux_rag_documents_post_file"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Лекция (Posts с заголовком "[LECTURE][class_id]...") — источник этого
    # документа. ON DELETE CASCADE: удалили лекцию — её RAG-данные исчезают
    # вместе с ней (посты/классы/чанки — см. crud/posts.py::delete_post).
    post_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Денормализовано для быстрой фильтрации без парсинга Posts.title на
    # каждый поиск — тот же приём, что у AiUsageLog.class_id/AiMessage.class_id.
    class_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    file_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SHA-256 исходных байт/текста — пропускаем повторную генерацию
    # эмбеддингов, если содержимое не изменилось с прошлого инжеста.
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    chunks: Mapped[list["RagChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        order_by="RagChunk.chunk_index",
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
    # JSON-массив float — единственный источник истины на SQLite. На
    # Postgres после миграции 018 параллельно заполняется embedding_vec
    # (VECTOR(1536), вне ORM-модели — как и было задумано в 002_rag_pgvector.sql)
    # для ANN-поиска через pgvector; эта колонка остаётся фолбэком/источником
    # правды для (ре)миграции индекса.
    embedding: Mapped[str] = mapped_column(Text, nullable=False)

    # Денормализовано с RagDocument — поиск идёт по rag_chunks напрямую, без
    # JOIN на каждый запрос (та же логика денормализации, что и выше).
    class_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    post_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    # Номер страницы источника (PDF) — NULL, если неприменимо (docx-текст,
    # подпись картинки и т.п.).
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # "text" (обычный текст/OCR) | "image_caption" (vision-подпись картинки:
    # скриншот/скан/диаграмма/формула — см. services/rag_ingest.py).
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text", server_default="text")

    document: Mapped["RagDocument"] = relationship(back_populates="chunks")

class AiUsageLog(Base):
    __tablename__ = "ai_usage_logs"
    __table_args__ = (
        # Дневная квота сообщений ИИ считает строки за сутки по пользователю.
        Index("ix_ai_usage_logs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    class_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class AiThread(Base):
    """Именованный тред (диалог) главного ассистента «Chatra AI» (class_id IS
    NULL). Позволяет пользователю вести несколько независимых, именуемых и
    закрепляемых бесед, синхронизируемых между устройствами. У ИИ-репетиторов
    класса (class_id задан) тредов нет — там ровно один неявный диалог на class_id."""
    __tablename__ = "ai_threads"
    __table_args__ = (
        Index("ix_ai_threads_user_sort", "user_id", "pinned", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="Новый чат")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AiMessage(Base):
    """Сообщение переписки с ИИ, сохранённое на сервере — чтобы история
    синхронизировалась между приложением и сайтом на всех устройствах.

    Главный ассистент (class_id IS NULL): сообщение принадлежит явному
    thread_id (обязателен начиная с версии с мульти-чатами — проверяется в
    роутере, а НЕ NOT NULL на уровне БД, чтобы не ломать до-миграционные строки).
    ИИ-репетитор класса (class_id задан): thread_id остаётся NULL ровно как
    раньше, поведение не меняется — тред = все сообщения (user_id, class_id)."""
    __tablename__ = "ai_messages"
    __table_args__ = (
        Index("ix_ai_messages_thread", "user_id", "class_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    class_id: Mapped[int] = mapped_column(Integer, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_threads.id", ondelete="CASCADE"), nullable=True, index=True
    )
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


class UserBlock(Base):
    """Блокировка одного пользователя другим (UGC Guideline 1.2).

    До этого блок-лист жил только в SharedPreferences на устройстве и
    сбрасывался при переустановке. Видимость взаимная: заблокированный не
    видит контент заблокировавшего и наоборот — см. services/moderation.py.
    """
    __tablename__ = "user_blocks"
    __table_args__ = (
        UniqueConstraint("user_id", "blocked_user_id", name="ux_user_blocks_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blocked_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Report(Base):
    """Жалоба на UGC-объект (пост, задание, сдача, сообщение ИИ, пользователь).

    Одна жалоба на пару (жалующийся, объект) — повторная подача отдаётся
    клиенту как 409 report_already, а не как ошибка. resolved закрывается
    админом из очереди модерации (реакция в течение 24 часов — требование
    App Store 1.2 / Google Play UGC).
    """
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "reporter_id", "target_type", "target_id", name="ux_reports_reporter_target"
        ),
        Index("ix_reports_open", "resolved", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    reporter_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Организация берётся из жалующегося: очередь модерации изолирована по
    # org_type, как и остальные админские выборки (BE-3).
    org_type: Mapped[str] = mapped_column(String, nullable=False, default="university")
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # dismissed | content_removed | user_blocked — как закрыли жалобу.
    resolution: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Annotation(Base):
    """Выделение текста в материале лекции: цвет, заметка и место в тексте.

    Общая сущность для приложения и сайта — выделение, сделанное на телефоне,
    должно открыться на сайте и наоборот, поэтому хранится на сервере, а не в
    локальном хранилище клиента.

    Про позицию. Экраны и рендереры у клиентов разные (PDF в pdf.js на сайте,
    свой текст лекции в приложении), поэтому никаких координат/пикселей: место
    описывается ТОЛЬКО текстом — смещения start_offset/end_offset в потоке
    текста «поверхности» (страница PDF, тело лекции) плюс якорь по самому
    тексту (prefix/selected_text/suffix, как TextQuoteSelector из W3C Web
    Annotation). Смещения дают точное попадание, когда документ отрисован так
    же, а prefix/suffix позволяют найти фрагмент заново, если разметка
    разъехалась (другой рендерер, другая версия файла) — без этого выделение
    молча «съезжало» бы на соседний текст.

    surface_key = страница PDF (1..N) либо 0 для документов без страниц
    (тело лекции, docx, txt) — вместе с file_index задаёт поверхность внутри
    лекции.
    """
    __tablename__ = "annotations"
    __table_args__ = (
        Index("ix_annotations_user_lecture", "user_id", "lecture_id"),
        Index("ix_annotations_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Пост-лекция, к которой относится выделение. CASCADE: удалили лекцию —
    # выделениям в ней не на что ссылаться.
    lecture_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Класс лекции — по нему проверяется доступ и собирается контекст для ИИ.
    class_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Индекс вложения внутри лекции; -1 — текст самой лекции (у него файла нет).
    file_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_text: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suffix: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="yellow")
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
