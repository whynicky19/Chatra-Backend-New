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
MAX_DESCRIPTION_LEN = 5000
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

    model_config = ConfigDict(from_attributes=True)

class CriterionIn(BaseModel):
    name: str = Field(max_length=MAX_NAME_LEN)
    # BE-5: вес критерия не может быть отрицательным (сумма весов формирует
    # максимум и проценты рейтинга).
    weight: int = Field(ge=0, le=1000)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)

class AssignmentCreate(BaseModel):
    class_id: int
    title: str = Field(max_length=MAX_TITLE_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    criteria: List[CriterionIn]
    # BE-5: max_score строго положителен — 0/отрицательный ломает деление в
    # проценты рейтинга (my_rating) и обесмысливает клампинг оценки.
    max_score: int = Field(default=100, ge=1, le=1000)
    deadline: Optional[datetime] = None
    reference_solution_url: Optional[str] = None

class AssignmentUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=MAX_TITLE_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    criteria: Optional[List[CriterionIn]] = None
    max_score: Optional[int] = Field(default=None, ge=1, le=1000)
    deadline: Optional[datetime] = None
    is_active: Optional[bool] = None
    reference_solution_url: Optional[str] = None

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

class RagIngestResponse(BaseModel):
    document_id: int
    filename: str
    chunks_created: int

class RagQueryRequest(BaseModel):
    question: str
    top_k: Optional[int] = None

class RagChunkSource(BaseModel):
    document_id: int
    filename: str
    chunk_index: int
    text_preview: str

class RagQueryResponse(BaseModel):
    answer: str
    sources: List[RagChunkSource]
    context_tokens: int

class ProcessedDocumentResponse(BaseModel):
    id: int
    rag_document_id: Optional[int]
    filename: str
    format: str
    token_count: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ClassCreate(BaseModel):
    name: str = Field(max_length=MAX_NAME_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    cover_image: Optional[str] = None
    teacher: Optional[str] = Field(default=None, max_length=200)
    period: Optional[str] = Field(default=None, max_length=100)

class ClassUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LEN)
    is_active: Optional[bool] = None
    cover_image: Optional[str] = None
    teacher: Optional[str] = Field(default=None, max_length=200)
    period: Optional[str] = Field(default=None, max_length=100)

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
    teacher: Optional[str] = None
    period: Optional[str] = None
    rotation_mode: str = "manual"
    # True — пользователь состоит только в архивных потоках класса:
    # класс для него read-only (сдача работ вернёт 403).
    is_archived_for_user: bool = False

    model_config = ConfigDict(from_attributes=True)

class ClassMemberAdd(BaseModel):
    user_id: int

class ClassJoinByCode(BaseModel):
    code: str

class InviteCodeResponse(BaseModel):
    invite_code: str

class VariantCreate(BaseModel):
    variant_number: int
    title: Optional[str] = None
    reference_solution_url: str

class VariantResponse(BaseModel):
    id: int
    assignment_id: int
    variant_number: int
    title: Optional[str] = None
    reference_solution_url: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AssignmentResponseFull(AssignmentResponse):
    variants: List[VariantResponse] = []

    model_config = ConfigDict(from_attributes=True)

class SubmissionCreateV2(BaseModel):
    text_content: Optional[str] = Field(default=None, max_length=MAX_SUBMISSION_TEXT_LEN)
    file_url: Optional[str] = None
    file_urls: Optional[List[str]] = None
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


