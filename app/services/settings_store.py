"""Настройки времени выполнения: значения из БД перекрывают .env.

Храним простым key-value, чтобы лимиты рассылки можно было менять из панели
без перезапуска и без правки конфигов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Setting

log = logging.getLogger(__name__)

#: какие ключи можно менять из панели и как их приводить к типу
FLOAT_KEYS = ("delay_min", "delay_max")
INT_KEYS = ("max_retries", "hourly_cap", "daily_cap")
BOOL_KEYS = ("auto_opt_out",)
STR_KEYS = ("stop_words",)
EDITABLE_KEYS: tuple[str, ...] = FLOAT_KEYS + INT_KEYS + BOOL_KEYS + STR_KEYS


@dataclass(slots=True)
class Effective:
    """Итоговые (эффективные) настройки рассылки."""

    delay_min: float = 3.0
    delay_max: float = 8.0
    max_retries: int = 3
    hourly_cap: int = 120
    daily_cap: int = 500
    auto_opt_out: bool = True
    stop_words: list[str] = field(default_factory=list)


def _defaults() -> dict[str, str]:
    return {
        "delay_min": str(settings.delay_min),
        "delay_max": str(settings.delay_max),
        "max_retries": str(settings.max_retries),
        "hourly_cap": str(settings.hourly_cap),
        "daily_cap": str(settings.daily_cap),
        "auto_opt_out": str(settings.auto_opt_out).lower(),
        "stop_words": settings.stop_words,
    }


async def get_raw(session: AsyncSession) -> dict[str, str]:
    """Значения из БД поверх значений .env."""
    values = _defaults()
    result = await session.execute(select(Setting))
    for row in result.scalars().all():
        if row.key in EDITABLE_KEYS:
            values[row.key] = row.value
    return values


def _cast(key: str, value: str, fallback: str) -> object:
    try:
        if key in FLOAT_KEYS:
            return max(0.0, float(value))
        if key in INT_KEYS:
            return max(0, int(value))
        if key in BOOL_KEYS:
            return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}
        return value
    except (TypeError, ValueError):
        log.warning("Настройка %s=%r не распознана, беру %r", key, value, fallback)
        return _cast(key, fallback, fallback) if key in FLOAT_KEYS + INT_KEYS + BOOL_KEYS else fallback


async def effective(session: AsyncSession) -> Effective:
    """Типизированные настройки для воркера рассылки."""
    raw = await get_raw(session)
    defaults = _defaults()
    values = {key: _cast(key, raw[key], defaults[key]) for key in EDITABLE_KEYS}
    stop_words = [w.strip().lower() for w in str(values["stop_words"]).split(",") if w.strip()]
    delay_min = float(values["delay_min"])
    delay_max = float(values["delay_max"])
    if delay_max < delay_min:  # защита от перепутанных полей в форме
        delay_min, delay_max = delay_max, delay_min
    return Effective(
        delay_min=delay_min,
        delay_max=delay_max,
        max_retries=int(values["max_retries"]),
        hourly_cap=int(values["hourly_cap"]),
        daily_cap=int(values["daily_cap"]),
        auto_opt_out=bool(values["auto_opt_out"]),
        stop_words=stop_words,
    )


async def set_values(session: AsyncSession, updates: dict[str, object]) -> dict[str, str]:
    """Сохраняет изменения настроек; неизвестные ключи игнорируются."""
    result = await session.execute(select(Setting))
    rows = {row.key: row for row in result.scalars().all()}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS or value is None or value == "":
            continue
        text = str(value).strip()
        if key in rows:
            rows[key].value = text
        else:
            rows[key] = Setting(key=key, value=text)
            session.add(rows[key])
    await session.commit()
    return await get_raw(session)
