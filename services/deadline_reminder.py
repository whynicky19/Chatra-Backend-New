"""Фоновый цикл: напоминание студентам за ~1 день до дедлайна задания.

Отдельно от deadline_checker (тот занимается авто-ИИ-оценкой ПОСЛЕ дедлайна).
Здесь — уведомление ДО: когда до due_date остаётся не больше 24 часов, шлём push
студентам потока, которые ещё не сдали работу. Повторную отправку глушит push_log
(dedup_key 'deadline:{deadline_id}' на пользователя) — на каждой итерации и после
рестарта уведомление уходит один раз.
"""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from db import SessionLocal
from models import Assignment, Deadline, Submission, PushLog
from crud.cohorts import get_cohort_student_ids
from services.fcm import send_push
from utils.time import utcnow

logger = logging.getLogger("deadline_reminder")

REMINDER_WINDOW_HOURS = 24
CHECK_INTERVAL_SECONDS = 30 * 60  # каждые полчаса


def _remind_one(db: Session, deadline: Deadline, assignment: Assignment) -> None:
    dedup_key = f"deadline:{deadline.id}"

    student_ids = get_cohort_student_ids(db, deadline.cohort_id)
    if not student_ids:
        return

    # Кто уже сдал этот дедлайн — тем не напоминаем.
    submitted = {
        row.student_id
        for row in db.query(Submission.student_id)
        .filter(Submission.deadline_id == deadline.id)
        .all()
    }
    # Кому уже отправляли напоминание по этому дедлайну.
    already = {
        row.user_id
        for row in db.query(PushLog.user_id)
        .filter(PushLog.dedup_key == dedup_key)
        .all()
    }

    targets = [uid for uid in student_ids if uid not in submitted and uid not in already]
    if not targets:
        return

    title = "Скоро дедлайн"
    body = f"«{assignment.title}» — сдать нужно завтра"
    data = {
        "type": "deadline",
        "notif_key": dedup_key,
        "assignment_id": assignment.id,
        "class_id": assignment.class_id,
    }

    # Сначала фиксируем факт отправки (дедуп), потом шлём: даже если процесс
    # упадёт между push_log и FCM, повторно спамить не будем.
    for uid in targets:
        db.add(PushLog(user_id=uid, dedup_key=dedup_key))
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — гонка двух воркеров: кто-то уже вставил
        db.rollback()
        return

    send_push(db, targets, title, body, data)
    logger.info("Напоминание о дедлайне #%s отправлено %s студ.", deadline.id, len(targets))


async def _check_reminders() -> None:
    db: Session = SessionLocal()
    try:
        now = utcnow()
        soon = now + timedelta(hours=REMINDER_WINDOW_HOURS)

        rows = (
            db.query(Deadline, Assignment)
            .join(Assignment, Assignment.id == Deadline.assignment_id)
            .filter(
                Deadline.is_published == True,  # noqa: E712
                Deadline.due_date > now,
                Deadline.due_date <= soon,
                Assignment.is_active == True,  # noqa: E712
            )
            .all()
        )
        for deadline, assignment in rows:
            try:
                _remind_one(db, deadline, assignment)
            except Exception as e:  # noqa: BLE001
                logger.error("Ошибка напоминания по дедлайну #%s: %s", deadline.id, e)
                db.rollback()
    except Exception as e:  # noqa: BLE001
        logger.error("Ошибка в deadline_reminder: %s", e)
    finally:
        db.close()


async def deadline_reminder_loop() -> None:
    logger.info("Deadline reminder запущен (интервал: %s сек)", CHECK_INTERVAL_SECONDS)
    while True:
        await _check_reminders()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
