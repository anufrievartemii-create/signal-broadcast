"""Синхронизация групп Signal в БД и выбор целевых групп для рассылки."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Campaign, Group, utcnow
from app.services import audit
from app.signal.base import SignalBackend

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    total: int = 0

    @property
    def summary(self) -> str:
        return f"новых {self.created}, обновлено {self.updated}, скрыто {self.deactivated}, всего {self.total}"


async def sync_account_groups(session: AsyncSession, backend: SignalBackend, account: Account) -> SyncResult:
    """Забирает группы аккаунта из Signal и обновляет локальный кэш.

    Группы, которых больше нет (или где мы не участник), не удаляем, а помечаем
    active=False — история рассылок по ним должна оставаться.
    """
    infos = await backend.list_groups(account.identifier)
    result = await session.execute(select(Group).where(Group.account_id == account.id))
    existing = {g.group_id: g for g in result.scalars().all()}
    seen: set[str] = set()
    stats_ = SyncResult(total=len(infos))

    for info in infos:
        seen.add(info.id)
        group = existing.get(info.id)
        if group is None:
            session.add(
                Group(
                    account_id=account.id,
                    group_id=info.id,
                    name=info.name,
                    description=info.description or None,
                    members_count=info.members_count,
                    is_member=info.is_member,
                    is_admin=account.identifier in info.admins,
                    active=info.is_member,
                    synced_at=utcnow(),
                )
            )
            stats_.created += 1
            continue
        group.name = info.name or group.name
        group.description = info.description or group.description
        group.members_count = info.members_count
        group.is_member = info.is_member
        group.is_admin = account.identifier in info.admins
        group.synced_at = utcnow()
        if not info.is_member and group.active:
            group.active = False
            stats_.deactivated += 1
        stats_.updated += 1

    for gid, group in existing.items():
        if gid not in seen and group.active:
            group.active = False
            stats_.deactivated += 1

    account.last_sync_at = utcnow()
    await session.commit()
    await audit.record(session, "groups.sync", f"аккаунт {account.identifier}: {stats_.summary}")
    return stats_


async def list_groups(
    session: AsyncSession,
    *,
    account_id: int | None = None,
    search: str | None = None,
    tag: str | None = None,
    only_active: bool = False,
) -> list[Group]:
    """Выборка групп с фильтрами для таблицы в панели."""
    stmt = select(Group).order_by(Group.name)
    if account_id:
        stmt = stmt.where(Group.account_id == account_id)
    if only_active:
        stmt = stmt.where(Group.active.is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(Group.name.ilike(pattern) | Group.description.ilike(pattern))
    if tag:
        stmt = stmt.where(Group.tags.contains(tag.strip()))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def all_tags(session: AsyncSession) -> list[str]:
    """Уникальные теги по всем группам (для фильтра)."""
    result = await session.execute(select(Group.tags))
    tags: set[str] = set()
    for (value,) in result.all():
        tags.update(t.strip() for t in (value or "").split(",") if t.strip())
    return sorted(tags)


async def bulk_update(
    session: AsyncSession,
    group_ids: list[int],
    *,
    active: bool | None = None,
    tags: str | None = None,
) -> int:
    """Массовые действия над группами: включить/выключить рассылку, проставить теги."""
    if not group_ids:
        return 0
    result = await session.execute(select(Group).where(Group.id.in_(group_ids)))
    groups = list(result.scalars().all())
    for group in groups:
        if active is not None:
            group.active = active
        if tags is not None:
            group.tags = ",".join(t.strip() for t in tags.split(",") if t.strip())
    await session.commit()
    await audit.record(session, "groups.bulk_update", f"{len(groups)} групп, active={active}, tags={tags!r}")
    return len(groups)


async def group_stats(session: AsyncSession, account_id: int | None = None) -> dict[str, int]:
    """Счётчики групп для дашборда."""
    base = select(func.count(Group.id))
    if account_id:
        base = base.where(Group.account_id == account_id)
    total = int((await session.execute(base)).scalar_one())
    active = int((await session.execute(base.where(Group.active.is_(True)))).scalar_one())
    return {"total": total, "active": active, "inactive": total - active}


def parse_target(campaign: Campaign) -> dict:
    """Целевая аудитория кампании из JSON-поля."""
    try:
        data = json.loads(campaign.target_json or "{}")
    except json.JSONDecodeError:
        log.warning("Кампания %s: невалидный target_json, считаю пустым", campaign.id)
        return {"mode": "selected", "group_ids": [], "tags": []}
    if not isinstance(data, dict):
        return {"mode": "selected", "group_ids": [], "tags": []}
    data.setdefault("mode", "selected")
    data.setdefault("group_ids", [])
    data.setdefault("tags", [])
    return data


async def resolve_targets(session: AsyncSession, campaign: Campaign) -> list[Group]:
    """Группы под рассылку с учётом режима выбора и отписок (active=False)."""
    target = parse_target(campaign)
    mode = target.get("mode", "selected")
    stmt = select(Group).where(
        Group.account_id == campaign.account_id,
        Group.active.is_(True),
        Group.is_member.is_(True),
    )
    if mode == "selected":
        ids = [int(i) for i in target.get("group_ids") or []]
        if not ids:
            return []
        stmt = stmt.where(Group.id.in_(ids))
    elif mode == "tags":
        tags = [str(t).strip() for t in target.get("tags") or [] if str(t).strip()]
        if tags:
            stmt = stmt.where(or_(*[Group.tags.contains(t) for t in tags]))
        else:
            return []
    elif mode != "all_active":
        log.warning("Кампания %s: неизвестный режим %r — беру все активные группы", campaign.id, mode)
    result = await session.execute(stmt.order_by(Group.name))
    return list(result.scalars().all())

