import asyncio
import json as _json
import logging
from datetime import timedelta
from utils.time import utcnow

from sqlalchemy.orm import Session

from db import SessionLocal
from crud import assignments as crud
from crud import posts as crud_posts
from models import Assignment, Deadline, Grade, Submission
from services.ai_grader import (
    grade_submission as _ai_grade,
    grade_handwritten_submission as _ai_grade_handwritten,
    collect_submission_material,
    AI_CONFIDENCE_THRESHOLD,
)

logger = logging.getLogger("deadline_checker")

# Сдача, висящая в "grading" дольше этого срока, считается зависшей (процесс упал
# между set_status и записью оценки) и возвращается в очередь. Порог с запасом
# больше самой долгой ИИ-проверки, чтобы не трогать активную.
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

        # Общий пайплайн с ручной кнопкой "Проверить ИИ"
        # (routers/assignments.py::ai_grade_submission): сбор текстов,
        # классификация фото/документы, встроенные картинки docx/pptx и
        # скан-PDF. Раньше логика дублировалась здесь построчно и рисковала
        # разойтись с ручным путём.
        full_text, image_urls, embedded_images = await collect_submission_material(
            text_content=submission.text_content,
            file_url=submission.file_url,
            file_urls_json=submission.file_urls,
        )

        if not full_text.strip() and not image_urls and not embedded_images:
            all_urls_hint = submission.file_urls or submission.file_url or ""
            full_text = f"[Студент сдал файл(ы), но прочитать не удалось: {all_urls_hint}]"

        lecture_context = crud_posts.get_lecture_context(db, assignment.class_id, limit=5)

        # crud.resolve_reference_solution_urls — общая логика с ручным путём
        # (routers/assignments.py), чтобы эталон резолвился одинаково для
        # автопроверки по дедлайну и ручной кнопки "Проверить ИИ".
        reference_urls = crud.resolve_reference_solution_urls(db, assignment, submission)

        if image_urls:
            result = await _ai_grade_handwritten(
                image_urls=image_urls,
                text=full_text,
                criteria=criteria,
                max_score=assignment.max_score,
                reference_solution_urls=reference_urls if reference_urls else None,
                lecture_context=lecture_context or None,
                extra_image_urls=embedded_images or None,
            )
        else:
            result = await _ai_grade(
                text=full_text,
                file_url=None,
                criteria=criteria,
                max_score=assignment.max_score,
                reference_solution_urls=reference_urls if reference_urls else None,
                lecture_context=lecture_context or None,
                embedded_image_urls=embedded_images or None,
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

        # Тот же гейт уверенности, что и у ручной кнопки "Проверить ИИ"
        # (routers/assignments.py::ai_grade_submission) — иначе автопроверка
        # по дедлайну публикует низкоуверенные/пустые сдачи без разбора.
        confidence = result.get("confidence")
        reasons = result.get("confidence_reasons")
        crud.set_submission_ai_confidence(db, sub_id, confidence, reasons)

        if confidence is None or confidence < AI_CONFIDENCE_THRESHOLD:
            crud.set_submission_status(db, sub_id, "needs_review")
            crud.create_or_update_grade(
                db=db,
                submission_id=sub_id,
                score=result["score"],
                feedback=result.get("feedback"),
                criteria_scores=result.get("criteria_scores"),
                graded_by="ai_suggested",
            )
            logger.info("Сдача %s ушла на ручную проверку (confidence=%s)", sub_id, confidence)
            try:
                from services.fcm import send_push_bg

                send_push_bg(
                    [submission.student_id],
                    "Работа на проверке",
                    f"«{assignment.title}» — проверяется учителем",
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
            return

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

        # Только дедлайны, по которым РЕАЛЬНО есть что оценивать. Раньше цикл
        # каждую минуту вытаскивал ВСЕ когда-либо истекшие дедлайны (их число
        # растёт с каждым учебным годом и никогда не убывает) и делал по
        # запросу сдач на каждый — при том, что _grade_batch для давно
        # проверенных заданий сразу же выходил, ничего не делая. Поведение то
        # же, работа — только по актуальной очереди.
        pending_deadline_ids = (
            db.query(Submission.deadline_id)
            .outerjoin(Grade, Grade.submission_id == Submission.id)
            .filter(
                Submission.deadline_id.isnot(None),
                Submission.status.in_(("submitted", "late")),
                Grade.id.is_(None),
            )
            .distinct()
        )
        expired_rows = (
            db.query(Deadline, Assignment)
            .join(Assignment, Assignment.id == Deadline.assignment_id)
            .filter(
                Deadline.is_published == True,
                Deadline.due_date <= now,
                Assignment.is_active == True,
                Deadline.id.in_(pending_deadline_ids),
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

        # Легаси-ветка (сдачи без deadline_id, дата в deprecated
        # assignments.deadline) — тот же приём: берём из БД только задания с
        # непроверенными сдачами, а не весь список заданий организации
        # целиком на каждой итерации.
        pending_assignment_ids = (
            db.query(Submission.assignment_id)
            .outerjoin(Grade, Grade.submission_id == Submission.id)
            .filter(
                Submission.deadline_id.is_(None),
                Submission.status.in_(("submitted", "late")),
                Grade.id.is_(None),
            )
            .distinct()
        )
        expired_legacy = (
            db.query(Assignment)
            .filter(
                Assignment.is_active == True,
                Assignment.deadline.isnot(None),
                Assignment.deadline <= now,
                Assignment.id.in_(pending_assignment_ids),
            )
            .all()
        )
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
