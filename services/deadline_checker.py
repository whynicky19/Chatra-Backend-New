import asyncio
import json as _json
import logging
from datetime import datetime, timedelta
from utils.time import utcnow

from sqlalchemy.orm import Session

from db import SessionLocal
from crud import assignments as crud
from models import Assignment, Deadline, Submission
from services.ai_grader import grade_submission as _ai_grade, _fetch_file_text

logger = logging.getLogger("deadline_checker")

# BE-2: сдача, застрявшая в "grading" дольше этого срока, считается зависшей
# (упал/рестартнулся процесс между set_status("grading") и записью оценки).
# Возвращаем её в очередь. Порог с запасом больше самой долгой ИИ-проверки
# (сеть + ретраи), чтобы не трогать активно проверяемую прямо сейчас работу.
GRADING_REAP_MINUTES = 15


def _reap_stale_grading(db: Session) -> None:
    """Возвращает зависшие в 'grading' сдачи (без оценки) в 'submitted', чтобы
    их подхватила обычная переоценка. Ориентир по submitted_at — отдельной
    метки времени входа в grading в схеме нет; порог большой, поэтому активную
    проверку это не задевает."""
    cutoff = utcnow() - timedelta(minutes=GRADING_REAP_MINUTES)
    stale = (
        db.query(Submission)
        .outerjoin(Submission.grade)
        .filter(
            Submission.status == "grading",
            Submission.submitted_at <= cutoff,
        )
        .all()
    )
    reaped = 0
    for sub in stale:
        if sub.grade is not None:
            # Оценка уже записана, а статус не успел обновиться — чиним статус.
            sub.status = "graded"
        else:
            sub.status = "submitted"
        reaped += 1
    if reaped:
        db.commit()
        logger.warning("Reaper: восстановлено %s зависших в 'grading' сдач(и)", reaped)

def _assignment_org(db: Session, assignment) -> str:
    """Организация задания = org его создателя (class_id исторически может
    ссылаться на легаси-посты, поэтому не через classes)."""
    from models import User
    creator = db.query(User).filter(User.id == assignment.created_by).first()
    return creator.org_type if creator else "university"


async def _grade_one(db: Session, submission, assignment, org_type: str = "university") -> None:
    sub_id = submission.id
    try:
        criteria = _json.loads(assignment.criteria) if assignment.criteria else []
        if not criteria:
            logger.warning("Задание %s: нет критериев, пропускаем сдачу %s", assignment.id, sub_id)
            return

        crud.set_submission_status(db, sub_id, "grading")

        full_text = submission.text_content or ""
        all_urls: list = []
        if submission.file_urls:
            try:
                all_urls = _json.loads(submission.file_urls)
            except Exception:
                pass
        if not all_urls and submission.file_url:
            all_urls = [submission.file_url]

        for i, url in enumerate(all_urls):
            file_text = await _fetch_file_text(url)
            if file_text.strip():
                label = f"ФАЙЛ {i+1}" if len(all_urls) > 1 else "СОДЕРЖИМОЕ ФАЙЛА"
                full_text = (
                    (full_text + f"\n\n{label}:\n" + file_text).strip()
                    if full_text
                    else f"{label}:\n{file_text}"
                )

        if not full_text:
            full_text = f"[Студент сдал файл(ы), но прочитать не удалось: {', '.join(all_urls)}]"

        result = await _ai_grade(
            text=full_text,
            file_url=None,
            criteria=criteria,
            max_score=assignment.max_score,
            reference_solution_url=assignment.reference_solution_url or None,
        )

        # BE-9: учитываем токены (иначе дневной бюджет не видел бы авто-проверки).
        try:
            usage = result.pop("_usage", {})
            from models import AiUsageLog
            db.add(AiUsageLog(
                user_id=assignment.created_by,
                class_id=assignment.class_id,
                endpoint="ai-grade-auto",
                org_type=org_type,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ))
            db.commit()
        except Exception:
            db.rollback()

        crud.set_submission_status(db, sub_id, "graded")
        crud.create_or_update_grade(
            db=db,
            submission_id=sub_id,
            score=result["score"],
            feedback=result.get("feedback"),
            criteria_scores=result.get("criteria_scores"),
            graded_by="ai",
        )
        logger.info("Сдача %s оценена ИИ: %s/%s", sub_id, result["score"], assignment.max_score)

        try:
            from services.fcm import send_push_bg

            send_push_bg(
                [submission.student_id],
                "Работа оценена",
                f"«{assignment.title}» — {result['score']}/{assignment.max_score}",
                {
                    "type": "grade",
                    "notif_key": f"grade:{sub_id}",
                    "submission_id": sub_id,
                    "assignment_id": assignment.id,
                    "class_id": assignment.class_id,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    except Exception as e:
        logger.error("Ошибка при оценке сдачи %s: %s", sub_id, e)
        try:
            crud.set_submission_status(db, sub_id, "submitted")
        except Exception:
            pass

async def _grade_batch(db: Session, submissions, assignment) -> None:
    ungraded = [
        s for s in submissions
        if s.status in ("submitted", "late") and not s.grade
    ]
    if not ungraded:
        return

    # BE-9: дневной бюджет токенов организации — не молотим дорогую очередь,
    # если лимит исчерпан; подхватим на следующих сутках.
    from services.ai_budget import can_spend
    org_type = _assignment_org(db, assignment)
    if not can_spend(db, org_type):
        logger.warning(
            "Задание '%s' (#%s): дневной бюджет ИИ (%s) исчерпан, откладываем %s сдач.",
            assignment.title, assignment.id, org_type, len(ungraded),
        )
        return

    logger.info(
        "Дедлайн задания '%s' (#%s) истёк. Запуск ИИ-оценки для %s сдач.",
        assignment.title, assignment.id, len(ungraded),
    )
    for submission in ungraded:
        await _grade_one(db, submission, assignment, org_type=org_type)
        await asyncio.sleep(1)
        # Бюджет мог исчерпаться внутри батча — проверяем перед каждой сдачей.
        if not can_spend(db, org_type):
            logger.warning(
                "Задание '%s' (#%s): бюджет ИИ исчерпан посреди батча, стоп.",
                assignment.title, assignment.id,
            )
            break

async def _check_deadlines() -> None:
    """Единый порядок чтения дедлайна (как в crud.cohorts.resolve_deadline):
    сначала записи deadlines по потокам — у одного задания их может быть
    несколько (по одной на поток), автопроверка срабатывает по каждой и
    оценивает только сдачи, заякоренные на этот дедлайн. Затем fallback:
    deprecated assignments.deadline для сдач без deadline_id (легаси)."""
    db: Session = SessionLocal()
    try:
        now = utcnow()

        # BE-2: сперва расшиваем зависшие в 'grading' сдачи (после падений).
        _reap_stale_grading(db)

        expired_rows = (
            db.query(Deadline, Assignment)
            .join(Assignment, Assignment.id == Deadline.assignment_id)
            .filter(
                Deadline.is_published == True,
                Deadline.due_date <= now,
                Assignment.is_active == True,
            )
            .all()
        )
        for deadline, assignment in expired_rows:
            submissions = (
                db.query(Submission)
                .filter(Submission.deadline_id == deadline.id)
                .all()
            )
            await _grade_batch(db, submissions, assignment)

        all_assignments = crud.get_all_assignments(db)
        expired_legacy = [
            a for a in all_assignments
            if a.deadline and a.deadline <= now and a.is_active
        ]
        for assignment in expired_legacy:
            submissions = (
                db.query(Submission)
                .filter(
                    Submission.assignment_id == assignment.id,
                    Submission.deadline_id.is_(None),
                )
                .all()
            )
            await _grade_batch(db, submissions, assignment)

    except Exception as e:
        logger.error("Ошибка в deadline_checker: %s", e)
    finally:
        db.close()

async def deadline_checker_loop() -> None:
    logger.info("Deadline checker запущен (интервал: 60 сек)")
    while True:
        await _check_deadlines()
        await asyncio.sleep(60)
