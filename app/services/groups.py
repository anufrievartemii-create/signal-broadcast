"""Синхронизация групп Signal в БД и выбор целевых групп для рассылки."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Campaign, Group, utcnow
from app.services import accounts as accounts_service
from app.services import audit
from app.signal.base import GroupInfo, SignalBackend, SignalError, accept_already_done

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    total: int = 0
    accepted: int = 0

    @property
    def summary(self) -> str:
        text = f"новых {self.created}, обновлено {self.updated}, скрыто {self.deactivated}, всего {self.total}"
        if self.accepted:
            return f"принято приглашений {self.accepted}, {text}"
        return text


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _account_matches_member(account: Account, member: str) -> bool:
    """Совпадает ли участник приглашения с номером или ACI нашего аккаунта."""
    member_text = member.strip().lower()
    member_digits = _digits(member)
    for candidate in (account.number, account.aci, account.identifier):
        if not candidate:
            continue
        if candidate.strip().lower() == member_text:
            return True
        digits = _digits(candidate)
        if digits and member_digits and digits == member_digits:
            return True
    return False


def is_pending_invite(info: GroupInfo, account: Account) -> bool:
    """Аккаунт уже приглашён в эту группу и ещё не участник.

    Чужие pending/requesting members (заявки других людей) сюда не попадают:
    мы не вступаем в группы без приглашения и не одобряем чужие заявки.
    """
    if info.is_member or not account.identifier:
        return False
    return any(_account_matches_member(account, member) for member in info.pending_members)


async def accept_pending_invites(
    session: AsyncSession,
    backend: SignalBackend,
    account: Account,
    *,
    pull_messages: bool = True,
) -> int:
    """Принимает приглашения, которые Signal уже показал этому аккаунту."""
    if not account.identifier:
        return 0
    if pull_messages:
        try:
            await backend.pull_messages(account.identifier)
        except SignalError as exc:
            log.info("Приём перед синхронизацией %s пропущен: %s", account.identifier, exc)
    infos = await backend.list_groups(account.identifier)
    accepted = 0
    for info in infos:
        if not is_pending_invite(info, account):
            continue
        try:
            await backend.accept_group_invite(account.identifier, info.id)
        except SignalError as exc:
            if accept_already_done(exc):
                log.info("Приглашение %s уже принято: %s", info.id, exc)
                continue
            log.warning("Не удалось принять приглашение %s (%s): %s", info.name or info.id, account.identifier, exc)
            continue
        accepted += 1
        log.info("Аккаунт %s принял приглашение в группу %s", account.identifier, info.name or info.id)
    if accepted:
        await audit.record(
            session,
            "groups.accept_invites",
            f"аккаунт {account.identifier}: принято {accepted}",
            commit=False,
        )
    return accepted


async def sync_account_groups(
    session: AsyncSession,
    backend: SignalBackend,
    account: Account,
    *,
    pull_messages: bool = True,
) -> SyncResult:
    """Принимает ожидающие приглашения и обновляет локальный кэш групп.

    Группы, которых больше нет (или где мы не участник), не удаляем, а помечаем
    active=False — история рассылок по ним должна оставаться.
    Ручное выключение рассылки (active=False при is_member=True) не включаем обратно.
    """
    accepted = await accept_pending_invites(session, backend, account, pull_messages=pull_messages)
    infos = await backend.list_groups(account.identifier)
    result = await session.execute(select(Group).where(Group.account_id == account.id))
    existing = {g.group_id: g for g in result.scalars().all()}
    seen: set[str] = set()
    stats_ = SyncResult(total=len(infos), accepted=accepted)

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
        group.is_admin = account.identifier in info.admins
        group.synced_at = utcnow()
        became_member = info.is_member and not group.is_member
        group.is_member = info.is_member
        if became_member:
            group.active = True
        elif not info.is_member and group.active:
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


async def sync_linked_accounts(session: AsyncSession, backend: SignalBackend) -> list[SyncResult]:
    """Принимает приглашения и синхронизирует группы всех привязанных аккаунтов."""
    results: list[SyncResult] = []
    for account in await accounts_service.list_linked(session):
        if not account.identifier:
            continue
        try:
            results.append(await sync_account_groups(session, backend, account))
        except SignalError as exc:
            log.warning("Синхронизация групп %s не удалась: %s", account.identifier, exc)
    return results


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

