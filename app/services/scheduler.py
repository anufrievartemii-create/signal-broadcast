"""Планировщик отложенных рассылок на APScheduler (AsyncIOScheduler)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Campaign, utcnow
from app.services import audit, broadcaster
from app.services import groups as groups_service
from app.signal import get_backend

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

GROUP_SYNC_JOB_ID = "groups-auto-accept"
_GROUP_SYNC_INTERVAL = timedelta(seconds=60)


def _job_id(campaign_id: int) -> str:
    return f"campaign-{campaign_id}"


async def _fire(campaign_id: int) -> None:
    """Тело задачи планировщика: запускает рассылку кампании."""
    log.info("Планировщик запускает кампанию %s", campaign_id)
    try:
        await broadcaster.start_campaign(campaign_id)
    except ValueError as exc:
        log.error("Не удалось запустить кампанию %s: %s", campaign_id, exc)
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is not None:
                campaign.status = "failed"
                campaign.finished_at = utcnow()
                await session.commit()
            await audit.record(session, "campaign.schedule_failed", f"{campaign_id}: {exc}")


async def _auto_accept_groups() -> None:
    """Периодически принимает приглашения и синхронизирует группы привязанных аккаунтов."""
    log.info("Автоприём приглашений и синхронизация групп")
    try:
        backend = get_backend()
    except ValueError as exc:
        log.error("Автоприём пропущен: %s", exc)
        return
    try:
        async with SessionLocal() as session:
            await groups_service.sync_linked_accounts(session, backend)
    except Exception:
        log.exception("Автоприём приглашений не удался")


def _ensure_auto_accept() -> None:
    """Ставит задачу автоприёма. Повторный start не плодит вторую задачу."""
    scheduler.add_job(
        _auto_accept_groups,
        trigger=IntervalTrigger(seconds=int(_GROUP_SYNC_INTERVAL.total_seconds())),
        id=GROUP_SYNC_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
        next_run_time=datetime.now(UTC) + timedelta(seconds=5),
    )


def start() -> None:
    """Запускает планировщик (вызывается из lifespan приложения)."""
    if not scheduler.running:
        scheduler.start()
        log.info("Планировщик запущен")
    _ensure_auto_accept()


def shutdown() -> None:
    """Останавливает планировщик при выключении приложения."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Планировщик остановлен")


async def schedule_campaign(campaign_id: int, when: datetime) -> str:
    """Ставит кампанию в расписание на указанное время."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC).astimezone()
    job = scheduler.add_job(
        _fire,
        trigger=DateTrigger(run_date=when),
        id=_job_id(campaign_id),
        replace_existing=True,
        args=[campaign_id],
        misfire_grace_time=3600,
    )
    async with SessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is not None:
            campaign.status = "scheduled"
            campaign.schedule_at = when
            await session.commit()
        await audit.record(session, "campaign.scheduled", f"{campaign_id} на {when.isoformat()}")
    return job.id


async def cancel_schedule(campaign_id: int) -> bool:
    """Снимает кампанию с расписания."""
    job_id = _job_id(campaign_id)
    removed = scheduler.get_job(job_id) is not None
    if removed:
        scheduler.remove_job(job_id)
    async with SessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is not None:
            if campaign.status == "scheduled":
                campaign.status = "draft"
            campaign.schedule_at = None
            await session.commit()
        await audit.record(session, "campaign.unscheduled", f"{campaign_id} (job был: {removed})")
    return removed


async def resync_pending() -> int:
    """После перезапуска приложения восстанавливает задачи для запланированных кампаний."""
    now = utcnow()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Campaign).where(Campaign.status == "scheduled", Campaign.schedule_at.is_not(None))
        )
        campaigns = list(result.scalars().all())
    restored = 0
    for campaign in campaigns:
        when = campaign.schedule_at
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when <= now:
            log.info("Кампания %s просрочена — запускаю сразу", campaign.id)
            await _fire(campaign.id)
        else:
            scheduler.add_job(
                _fire,
                trigger=DateTrigger(run_date=when),
                id=_job_id(campaign.id),
                replace_existing=True,
                args=[campaign.id],
                misfire_grace_time=3600,
            )
        restored += 1
    if restored:
        log.info("Восстановлено запланированных кампаний: %s", restored)
    return restored


def list_jobs() -> list[dict]:
    """Задачи планировщика для экрана настроек/кампаний."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "name": job.name,
            }
        )
    return jobs
