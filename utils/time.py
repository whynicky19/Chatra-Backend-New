"""Единая точка получения времени.

Колонки DateTime в моделях — naive (без tzinfo) и хранят UTC. datetime.utcnow()
объявлен deprecated в Python 3.12+, поэтому используем tz-aware now(UTC) и
сбрасываем tzinfo, сохраняя прежнюю naive-UTC семантику (сравнения с naive-
значениями из БД остаются корректными).
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp — замена устаревшему datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
