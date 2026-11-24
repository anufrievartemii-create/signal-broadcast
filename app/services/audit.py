"""Журнал действий: единая точка записи AuditLog."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

log = logging.getLogger(__name__)


async def record(session: AsyncSession, action: str, detail: str | None = None, *, commit: bool = True) -> None:
    """Пишет строку в audit_log. commit=False — если вызывающий код сам коммитит."""
    session.add(AuditLog(action=action, detail=(detail or "")[:2000] or None))
    if commit:
        await session.commit()
    else:
        await session.flush()
    log.info("audit: %s %s", action, (detail or "")[:200])


async def recent(session: AsyncSession, limit: int = 50, action: str | None = None) -> list[AuditLog]:
    """Последние записи журнала (новые сверху)."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    result = await session.execute(stmt)
    return list(result.scalars().all())
