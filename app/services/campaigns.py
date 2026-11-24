"""CRUD кампаний, предпросмотр текста и экспорт отчёта."""

from __future__ import annotations

import csv
import io
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BroadcastItem, Campaign
from app.schemas import CampaignForm
from app.services import audit
from app.services import groups as groups_service
from app.services.templates import Preview, build_context, check_variables, render

log = logging.getLogger(__name__)


def _target_json(form: CampaignForm) -> str:
    return json.dumps(
        {"mode": form.target_mode, "group_ids": form.group_ids, "tags": form.tags},
        ensure_ascii=False,
    )


def _apply_form(campaign: Campaign, form: CampaignForm) -> None:
    """Переносит данные формы в модель кампании."""
    campaign.name = form.name.strip()
    campaign.account_id = form.account_id
    campaign.body_template = form.body_template
    campaign.variables_json = json.dumps(form.variables or {}, ensure_ascii=False)
    campaign.attachments_json = json.dumps(form.attachments or [], ensure_ascii=False)
    campaign.target_json = _target_json(form)
    campaign.schedule_at = form.schedule_at
    campaign.dry_run = form.dry_run
    if form.delay_min is not None:
        campaign.delay_min = form.delay_min
    if form.delay_max is not None:
        campaign.delay_max = form.delay_max
    campaign.hourly_cap = form.hourly_cap
    campaign.daily_cap = form.daily_cap


async def list_campaigns(
    session: AsyncSession, *, account_id: int | None = None, status: str | None = None
) -> list[Campaign]:
    stmt = select(Campaign).order_by(Campaign.id.desc())
    if account_id:
        stmt = stmt.where(Campaign.account_id == account_id)
    if status:
        stmt = stmt.where(Campaign.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def get(session: AsyncSession, campaign_id: int) -> Campaign | None:
    return await session.get(Campaign, campaign_id)


async def create(session: AsyncSession, form: CampaignForm) -> Campaign:
    campaign = Campaign(status="draft")
    _apply_form(campaign, form)
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    await audit.record(session, "campaign.created", f"{campaign.id} {campaign.name!r}")
    return campaign


async def update(session: AsyncSession, campaign_id: int, form: CampaignForm) -> Campaign | None:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    if campaign.status == "running":
        msg = "Нельзя менять кампанию во время рассылки — сначала остановите её"
        raise ValueError(msg)
    _apply_form(campaign, form)
    await session.commit()
    await audit.record(session, "campaign.updated", f"{campaign.id} {campaign.name!r}")
    return campaign


async def delete(session: AsyncSession, campaign_id: int) -> bool:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return False
    await audit.record(session, "campaign.deleted", f"{campaign.id} {campaign.name!r}")
    await session.delete(campaign)
    await session.commit()
    return True


async def item_counts(session: AsyncSession, campaign_id: int) -> dict[str, int]:
    """Счётчики позиций кампании по статусам."""
    stmt = (
        select(BroadcastItem.status, func.count(BroadcastItem.id))
        .where(BroadcastItem.campaign_id == campaign_id)
        .group_by(BroadcastItem.status)
    )
    counts = {status: 0 for status in ("pending", "sent", "failed", "skipped")}
    for status, count in (await session.execute(stmt)).all():
        counts[str(status)] = int(count)
    counts["total"] = sum(counts.values())
    return counts


async def list_items(
    session: AsyncSession, campaign_id: int, *, status: str | None = None
) -> list[BroadcastItem]:
    stmt = select(BroadcastItem).where(BroadcastItem.campaign_id == campaign_id).order_by(BroadcastItem.id)
    if status:
        stmt = stmt.where(BroadcastItem.status == status)
    return list((await session.execute(stmt)).scalars().all())


def export_csv(campaign: Campaign, items: list[BroadcastItem]) -> str:
    """Отчёт по кампании в CSV (Excel-friendly: разделитель ; и BOM)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Кампания", campaign.name])
    writer.writerow(["Статус", campaign.status])
    writer.writerow([])
    writer.writerow(["Группа", "Group ID", "Статус", "Попыток", "Ошибка", "Timestamp Signal", "Отправлено"])
    for item in items:
        writer.writerow(
            [
                item.group_name,
                item.group_gid,
                item.status,
                item.attempts,
                item.error or "",
                item.signal_timestamp or "",
                item.sent_at.isoformat() if item.sent_at else "",
            ]
        )
    return "\ufeff" + buffer.getvalue()


async def preview(session: AsyncSession, form: CampaignForm, *, limit: int = 5) -> list[Preview]:
    """Предпросмотр рендера для первых групп — до реальной отправки.

    Используем временный (не сохранённый) объект кампании: resolve_targets
    читает только account_id и target_json, поэтому в БД ничего не пишется.
    """
    account = await session.get(Account, form.account_id)
    transient = Campaign(
        name=form.name,
        account_id=form.account_id,
        target_json=_target_json(form),
        body_template=form.body_template,
    )
    groups = await groups_service.resolve_targets(session, transient)
    previews: list[Preview] = []
    for group in groups[:limit]:
        context = build_context(group, form.variables or {}, account)
        text = render(form.body_template, context, strict=False)
        previews.append(Preview(group=group, text=text, length=len(text)))
    return previews


def missing_variables(form: CampaignForm) -> list[str]:
    """Переменные шаблона, которые не заданы вручную и не подставляются автоматически."""
    return check_variables(form.body_template, form.variables or {})

