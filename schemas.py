from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from typing import Optional, List, Any
from datetime import date, datetime

# Единственный источник правды по допустимым ролям — валидируется везде,
# где роль приходит от клиента (регистрация запрещает её вовсе).
ALLOWED_ROLES = ("student", "teacher", "admin")


def validate_role(value: str) -> str:
    if value not in ALLOWED_ROLES:
        raise ValueError(f"role must be one of {ALLOWED_ROLES}")
    return value


class UserRegister(BaseModel):
    """Схема самостоятельной регистрации: роль клиент задавать не может."""
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = Field(default=None, max_length=200)
    org_type: str = "university"

    model_config = ConfigDict(from_attributes=True)

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str
    full_name: Optional[str] = Field(default=None, max_length=200)
    org_type: str = "university"

    _role_valid = field_validator("role")(validate_role)

    model_config = ConfigDict(from_attributes=True)

class UserResponse(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    role: str
    full_name: Optional[str] = Field(default=None, max_length=200)
    org_type: str = "university"
    ai_unlimited: bool = False
    # NULL у аккаунтов, зарегистрированных до migrations/022 — клиент показывает
    # «—», а не выдумывает дату.
    created_at: Optional[datetime] = None
    # False = аккаунт создан, но письмо не ушло (SMTP лёг). Клиент обязан сказать
    # об этом, иначе юзер ждёт код, которого не будет.
    email_sent: bool = True

    model_config = ConfigDict(from_attributes=True)


class AiUnlimitedUpdate(BaseModel):
    unlimited: bool

class UpdateMe(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=200)

class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)

class DeleteAccount(BaseModel):
    # Подтверждение паролем — защита от удаления по украденному access-токену.
    password: str

class EmailCodeRequest(BaseModel):
    """Запрос кода: forgot-password и resend-verification."""
    email: EmailStr
    org_type: str = "university"

class VerifyEmailRequest(BaseModel):
    email: EmailStr
    org_type: str = "university"
    code: str = Field(min_length=4, max_length=8)

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    org_type: str = "university"
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=8)

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class UserAdminUpdate(BaseModel):
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _role_valid(cls, v):
        return None if v is None else validate_role(v)

# Верхние границы пользовательских строк: без них можно прислать мегабайты —
# раздувает БД, а текст сдачи ещё и целиком улетает в GPT (BE-6).
MAX_TITLE_LEN = 500
MAX_NAME_LEN = 256
# Ссылки на прикреплённые файлы кладутся прямо в текст description (см.
# lib/screens/classes/class_detail_screen.dart, descWithFiles) — один
# подписанный R2 URL с exp/sig и кириллическим именем в fragment легко
# занимает 300-400 символов. При 10 файлах (kMaxFilesPerPost) старого лимита
# 5000 не хватало: PUT/POST /assignments падал 422, а с точки зрения
# пользователя выглядело как "файлы не добавляются".
MAX_DESCRIPTION_LEN = 20000
MAX_POST_BODY_LEN = 100_000
MAX_SUBMISSION_TEXT_LEN = 100_000


class PostCreate(BaseModel):
    title: str = Field(max_length=MAX_TITLE_LEN)
    body: str = Field(max_length=MAX_POST_BODY_LEN)

class PostResponse(BaseModel):
    id: int
    title: str
    body: str
    user_id: int
    created_at: Optional[datetime] = None
    position: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

class CriterionIn(BaseModel):
    name: str = Field(max_length=MAX_NAME_LEN)
    # BE-5: вес критерия не может быть отрицательным (сумма весов формирует
    # максимум и проценты рейтинга).
    weight: int = Field(ge=0, le=1000)

class AssignmentCreate(BaseModel):
    class_id: int
    title: str = Field(max_length=MAX_TITLE_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    criteria: List[CriterionIn]
    deadline: Optional[datetime] = None
    reference_solution_url: Optional[str] = None

class AssignmentUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    criteria: Optional[List[CriterionIn]] = None
    deadline: Optional[datetime] = None
    is_active: Optional[bool] = None
    reference_solution_url: Optional[str] = None
    # body.model_dump(exclude_none=True) в роуте отбрасывает deadline=None —
    # значит просто прислать null нельзя было отличить от "не менять".
    # Явный флаг — как пустая строка для reference_solution_url (см. коммент
    # там же), но датам пустую строку не передать (не парсится в datetime).
    clear_deadline: bool = False

class AssignmentResponse(BaseModel):
    id: int
    class_id: int
    title: str
    description: Optional[str] = None
    criteria: str
    max_score: int
    deadline: Optional[datetime] = None
    created_at: datetime
    is_active: bool
    created_by: int
    reference_solution_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class SubmissionCreate(BaseModel):
    text_content: Optional[str] = Field(default=None, max_length=MAX_SUBMISSION_TEXT_LEN)
    file_url: Optional[str] = None
    file_urls: Optional[List[str]] = None

class SubmissionResponse(BaseModel):
    id: int
    assignment_id: int
    student_id: int
    file_url: Optional[str] = None
    file_urls: Optional[str] = None
    text_content: Optional[str] = None
    variant_number: Optional[int] = None
    submitted_at: datetime
    status: str
    student_name: Optional[str] = None
    # Только для учителя: teacher-эндпоинты возвращают как есть, студенческие
    # (my-submissions, GET своей сдачи) обнуляют перед отдачей — см. роутер.
    ai_confidence: Optional[int] = None
    ai_review_reasons: Optional[str] = None  # JSON-список строк, как criteria_scores

    model_config = ConfigDict(from_attributes=True)

class SubmissionWithGrade(SubmissionResponse):
    grade: Optional["GradeResponse"] = None

    model_config = ConfigDict(from_attributes=True)

class GradeCreate(BaseModel):
    # graded_by не принимаем — сервер ставит сам.
    # Верхнюю границу клампит save_grade: max_score задания здесь неизвестен.
    score: int = Field(ge=0)
    feedback: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    criteria_scores: Optional[List[Any]] = None

class GradeResponse(BaseModel):
    id: int
    submission_id: int
    score: int
    feedback: Optional[str] = None
    criteria_scores: Optional[str] = None
    graded_at: datetime
    graded_by: str

    model_config = ConfigDict(from_attributes=True)

SubmissionWithGrade.model_rebuild()

class AiGradeResult(BaseModel):
    """Ответ POST /submissions/{id}/ai-grade.

    status="needs_review" — уверенность ИИ в оценке ниже AI_CONFIDENCE_THRESHOLD
    (для любого типа сдачи): grade содержит предложение ИИ (graded_by=
    "ai_suggested", видно только учителю), финальную оценку выставляет учитель
    через POST /submissions/{id}/grade (подтверждение или правка).
    """
    status: str  # "graded" | "needs_review"
    grade: Optional[GradeResponse] = None
    ai_confidence: Optional[int] = None
    ai_review_reasons: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

def validate_cover_color(value: Optional[str]) -> Optional[str]:
    """Слаг цвета обложки. Опечатку возвращаем 422, а не молча подменяем
    дефолтом — иначе преподаватель выбрал бы один цвет, а получил другой."""
    from services.cover_art import PALETTE

    if value is None:
        return None
    key = value.strip().lower()
    if key not in PALETTE:
        raise ValueError(f"cover_color must be one of {tuple(PALETTE)}")
    return key


def validate_cover_icon(value: Optional[str]) -> Optional[str]:
    from services.cover_art import ICONS

    if value is None:
        return None
    key = value.strip().lower()
    if key not in ICONS:
        raise ValueError(f"cover_icon must be one of {tuple(ICONS)}")
    return key


class ClassCreate(BaseModel):
    """Новые классы оформляются только парой «цвет + иконка»: обложку рисует
    бэкенд (см. services/cover_generator.py), загрузки картинки здесь больше
    нет. Если цвет с иконкой не переданы, берутся значения по умолчанию —
    класс не может появиться без обложки."""
    name: str = Field(max_length=MAX_NAME_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    cover_color: Optional[str] = None
    cover_icon: Optional[str] = None
    teacher: Optional[str] = Field(default=None, max_length=200)
    period: Optional[str] = Field(default=None, max_length=100)

    _v_color = field_validator("cover_color")(validate_cover_color)
    _v_icon = field_validator("cover_icon")(validate_cover_icon)

class ClassUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    is_active: Optional[bool] = None
    # Смена цвета/иконки перерисовывает обложку локальным фолбэком (мгновенно
    # и бесплатно), чтобы она не разъезжалась с выбором. Обращение к модели —
    # только явной кнопкой, POST /classes/{id}/cover/generate.
    cover_color: Optional[str] = None
    cover_icon: Optional[str] = None
    # Легаси: путь загрузки своей картинки. Клиенты Chatra его больше не
    # используют, но поле оставлено, чтобы уже установленные у пользователей
    # старые сборки приложения не теряли обложку при редактировании класса.
    cover_image: Optional[str] = None
    teacher: Optional[str] = Field(default=None, max_length=200)
    period: Optional[str] = Field(default=None, max_length=100)

    _v_color = field_validator("cover_color")(validate_cover_color)
    _v_icon = field_validator("cover_icon")(validate_cover_icon)

class ClassResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    created_by: int
    created_at: datetime
    is_active: bool
    member_count: Optional[int] = None
    invite_code: Optional[str] = None
    cover_image: Optional[str] = None
    cover_thumbnail: Optional[str] = None
    # NULL — обложка загружена пользователем по старой системе (см. models.py).
    cover_color: Optional[str] = None
    cover_icon: Optional[str] = None
    cover_source: Optional[str] = None
    teacher: Optional[str] = None
    period: Optional[str] = None
    rotation_mode: str = "manual"
    # True — пользователь состоит только в архивных потоках класса:
    # класс для него read-only (сдача работ вернёт 403).
    is_archived_for_user: bool = False

    model_config = ConfigDict(from_attributes=True)


class CoverGenerateRequest(BaseModel):
    """Тело POST /classes/{id}/cover/generate. Собственного промпта у
    преподавателя нет намеренно: единый стиль коллекции держится тем, что
    промпт целиком строит бэкенд (services/cover_art.py: build_prompt)."""
    color: Optional[str] = None
    icon: Optional[str] = None

    _v_color = field_validator("color")(validate_cover_color)
    _v_icon = field_validator("icon")(validate_cover_icon)


class CoverGenerateResponse(BaseModel):
    cover_image: Optional[str] = None
    cover_thumbnail: Optional[str] = None
    cover_color: Optional[str] = None
    cover_icon: Optional[str] = None
    # 'ai' — обложку нарисовала модель; 'fallback' — модель была недоступна и
    # обложку собрал сам сервис. Клиент по этому полю показывает мягкое
    # предупреждение и предлагает повторить.
    cover_source: Optional[str] = None


class CoverColorOption(BaseModel):
    id: str
    # Акцент бренда: свотч в пикере и подсветка выбора.
    hex: str
    # Светлая пастельная заливка фона — из неё клиент строит превью до генерации.
    base: str
    # Цвет предметной иконки поверх обложки. Иконка рисуется В ЦВЕТ обложки,
    # а не белой: на светлой пастели белая просто пропадает.
    ink: str


class CoverIconOption(BaseModel):
    id: str
    subject: str
    # Секция пикера: символов больше сорока, плоским списком их не листают.
    # Порядок в ответе уже сгруппирован — клиент, который про группы не знает,
    # просто выведет всё подряд и ничего не сломает.
    group: str = ""
    group_label: str = ""


class CoverIconGroup(BaseModel):
    id: str
    label: str


class CoverOptionsResponse(BaseModel):
    """Палитра и набор иконок для пикеров. Веб и приложение строят выбор из
    этого ответа, поэтому набор нигде не расходится с тем, что умеет рисовать
    бэкенд."""
    colors: List[CoverColorOption]
    icons: List[CoverIconOption]
    # Порядок секций пикера. Пустой список у старого клиента — обычный плоский
    # список символов, как было раньше.
    groups: List[CoverIconGroup] = []
    default_color: str
    default_icon: str
    # False — OPENAI_API_KEY на сервере не задан: генерация недоступна, все
    # обложки будут фолбэками. Клиент прячет кнопку «Сгенерировать».
    ai_available: bool = True

class ClassMemberAdd(BaseModel):
    user_id: int

class ClassJoinByCode(BaseModel):
    code: str

class InviteCodeResponse(BaseModel):
    invite_code: str

class SubmissionCreateV2(BaseModel):
    text_content: Optional[str] = Field(default=None, max_length=MAX_SUBMISSION_TEXT_LEN)
    file_url: Optional[str] = None
    # Клиентский лимит — 10 файлов на сдачу (upload_limits.dart); без max_length
    # здесь злоупотребление раздувало ai-grade (до 3 скачиваний на файл).
    file_urls: Optional[List[str]] = Field(default=None, max_length=10)
    variant_number: Optional[int] = None

class PublicUserResponse(BaseModel):
    id: int
    full_name: Optional[str] = None
    role: str
    org_type: str = "university"

    model_config = ConfigDict(from_attributes=True)


class StudentRatingEntry(BaseModel):
    student_id: int
    # SEC-4: e-mail одноклассников из рейтинга убран — отдаём только имя.
    full_name: str
    total_score: int
    graded_count: int
    avg_score: float

class StudentRatingResponse(BaseModel):
    class_id: Optional[int] = None
    ratings: List[StudentRatingEntry]




class CohortResponse(BaseModel):
    id: int
    class_id: int
    academic_year: str
    start_date: date
    status: str
    created_at: datetime
    student_count: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class DeadlineResponse(BaseModel):
    id: int
    cohort_id: int
    assignment_id: int
    # Название задания — чтобы экран проверки дедлайнов не показывал голые id.
    assignment_title: Optional[str] = None
    due_date: datetime
    is_published: bool

    model_config = ConfigDict(from_attributes=True)


class DeadlineUpdate(BaseModel):
    due_date: Optional[datetime] = None
    is_published: Optional[bool] = None


class RotationModeUpdate(BaseModel):
    rotation_mode: str

    @field_validator("rotation_mode")
    @classmethod
    def _mode_valid(cls, v):
        if v not in ("manual", "yearly"):
            raise ValueError("rotation_mode must be 'manual' or 'yearly'")
        return v


class RolloverPreviewItem(BaseModel):
    class_id: int
    class_name: str
    cohort_id: int
    academic_year: str
    student_count: int
    assignment_count: int


class RolloverRequest(BaseModel):
    class_ids: List[int]
    new_academic_year: str = Field(pattern=r"^\d{4}/\d{4}$")
    new_start_date: date


class RolloverResultItem(BaseModel):
    # status: rolled | already_rolled | no_active_cohort | conflict
    class_id: int
    status: str
    new_cohort_id: Optional[int] = None
    deadlines_created: int = 0


# ── Модерация UGC (App Store 1.2 / Google Play UGC) ────────────────────────
REPORT_TARGET_TYPES = ("post", "assignment", "submission", "user")
REPORT_REASONS = ("spam", "abuse", "inappropriate", "academic", "other")
REPORT_RESOLUTIONS = ("dismissed", "content_removed", "user_blocked")


class ReportCreate(BaseModel):
    target_type: str
    target_id: int
    reason: str
    comment: Optional[str] = Field(default=None, max_length=500)

    @field_validator("target_type")
    @classmethod
    def _target_type_valid(cls, v: str) -> str:
        if v not in REPORT_TARGET_TYPES:
            raise ValueError(f"target_type must be one of {REPORT_TARGET_TYPES}")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_valid(cls, v: str) -> str:
        if v not in REPORT_REASONS:
            raise ValueError(f"reason must be one of {REPORT_REASONS}")
        return v


class ReportResponse(BaseModel):
    id: int
    target_type: str
    target_id: int
    reason: str
    comment: Optional[str] = None
    reporter_name: Optional[str] = None
    reporter_email: Optional[str] = None
    created_at: Optional[datetime] = None
    resolved: bool = False
    # Куда указывает жалоба — заполняется на бэке при list_reports, чтобы
    # админ видел «Класс X → Лекция Y», а не голый target_type/target_id.
    class_id: Optional[int] = None
    class_name: Optional[str] = None
    target_title: Optional[str] = None
    author_id: Optional[int] = None
    author_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ReportResolve(BaseModel):
    # Необязательно: клиент может закрыть жалобу без указания причины.
    action: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _action_valid(cls, v):
        if v is not None and v not in REPORT_RESOLUTIONS:
            raise ValueError(f"action must be one of {REPORT_RESOLUTIONS}")
        return v


