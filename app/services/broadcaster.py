"""Воркер рассылки по группам.

Отвечает за: очередь позиций, случайные задержки (анти-бан), ретраи с backoff,
идемпотентность (campaign + группа), лимиты в час/день, паузу и аварийный стоп,
а также за прогресс-события для SSE в веб-панели.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Account, BroadcastItem, Campaign, Group, utcnow
from app.services import attachments, audit, settings_store, templates
from app.services import contacts as contacts_service
from app.services import groups as groups_service
from app.signal import get_backend
from app.signal.base import SignalBackend, SignalError, SignalFatalSend, SignalRateLimited, SignalUnavailable

log = logging.getLogger(__name__)

#: сколько событий прогресса держим в памяти для только что подключившихся клиентов
_EVENT_BACKLOG = 200


@dataclass
class RunState:
    """Состояние одного запущенного прогона кампании (в памяти процесса)."""

    campaign_id: int
    total: int = 0
    processed: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    state: str = "idle"  # idle | running | paused | aborting | finished
    current: str | None = None
    reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    backlog: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        # set() = не на паузе; clear() = пауза
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.abort_event = asyncio.Event()

    @property
    def is_active(self) -> bool:
        return self.task is not None and not self.task.done()

    def snapshot(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "total": self.total,
            "processed": self.processed,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "state": self.state,
            "current": self.current,
            "reason": self.reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "percent": round(100 * self.processed / self.total, 1) if self.total else 0.0,
        }

    def publish(self, event: dict) -> None:
        """Кладёт событие всем подписчикам SSE и в backlog."""
        event.setdefault("ts", utcnow().isoformat())
        self.backlog.append(event)
        if len(self.backlog) > _EVENT_BACKLOG:
            del self.backlog[: len(self.backlog) - _EVENT_BACKLOG]
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - медленный клиент
                log.debug("Очередь подписчика переполнена, событие пропущено")

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)


class RunRegistry:
    """Реестр активных прогонов. Один процесс uvicorn = одна общая таблица."""

    def __init__(self) -> None:
        self._runs: dict[int, RunState] = {}

    def get(self, campaign_id: int) -> RunState | None:
        return self._runs.get(campaign_id)

    def ensure(self, campaign_id: int) -> RunState:
        run = self._runs.get(campaign_id)
        if run is None:
            run = RunState(campaign_id=campaign_id)
            self._runs[campaign_id] = run
        return run

    def active(self) -> list[RunState]:
        return [run for run in self._runs.values() if run.is_active]

    def all(self) -> list[RunState]:
        return list(self._runs.values())

    def drop_finished(self) -> None:
        """Убирает завершённые прогоны, чтобы реестр не рос бесконечно."""
        for campaign_id in [cid for cid, run in self._runs.items() if not run.is_active and not run.subscribers]:
            if self._runs[campaign_id].state == "finished":
                del self._runs[campaign_id]


registry = RunRegistry()


# --- планирование позиций -----------------------------------------------------


def campaign_is_direct(campaign: Campaign) -> bool:
    return groups_service.parse_target(campaign).get("mode") == "direct"


async def plan_items(session, campaign: Campaign) -> tuple[list[BroadcastItem], list[Group]]:
    """Создаёт позиции рассылки. Идемпотентно.

    Позиция = пара (кампания, group_gid): для групп это ID группы Signal,
    для личных сообщений — номер получателя.
    """
    result = await session.execute(select(BroadcastItem).where(BroadcastItem.campaign_id == campaign.id))
    existing = {item.group_gid: item for item in result.scalars().all()}
    items: list[BroadcastItem] = []
    if campaign_is_direct(campaign):
        target = groups_service.parse_target(campaign)
        recipients = await contacts_service.resolve_recipients(
            session,
            account_id=campaign.account_id,
            numbers=[str(n) for n in target.get("recipients") or []],
            contact_ids=[int(i) for i in target.get("contact_ids") or []],
        )
        for number, name in recipients:
            item = existing.get(number)
            if item is None:
                item = BroadcastItem(
                    campaign_id=campaign.id,
                    group_gid=number,
                    group_name=name,
                    status="pending",
                )
                session.add(item)
            items.append(item)
        await session.commit()
        return items, []

    targets = await groups_service.resolve_targets(session, campaign)
    for group in targets:
        item = existing.get(group.group_id)
        if item is None:
            item = BroadcastItem(
                campaign_id=campaign.id,
                group_gid=group.group_id,
                group_name=group.name,
                status="pending",
            )
            session.add(item)
        items.append(item)
    await session.commit()
    return items, targets


async def reset_items(
    session,
    campaign_id: int,
    *,
    statuses: tuple[str, ...] = ("failed",),
    dry_run_only: bool = False,
) -> int:
    """Возвращает позиции в pending — например, чтобы повторить только упавшие."""
    stmt = select(BroadcastItem).where(
        BroadcastItem.campaign_id == campaign_id,
        BroadcastItem.status.in_(statuses),
    )
    if dry_run_only:
        stmt = stmt.where(BroadcastItem.error.like("dry-run%"))
    items = list((await session.execute(stmt)).scalars().all())
    for item in items:
        item.status = "pending"
        item.attempts = 0
        item.error = None
        item.signal_timestamp = None
        item.sent_at = None
    await session.commit()
    return len(items)


async def sent_counts(session, account_id: int) -> tuple[int, int]:
    """Сколько сообщений ушло с аккаунта за последний час и за сутки."""
    now = utcnow()
    base = (
        select(func.count(BroadcastItem.id))
        .join(Campaign, BroadcastItem.campaign_id == Campaign.id)
        .where(
            Campaign.account_id == account_id,
            BroadcastItem.status == "sent",
            BroadcastItem.sent_at.is_not(None),
        )
    )
    hourly = (await session.execute(base.where(BroadcastItem.sent_at >= now - timedelta(hours=1)))).scalar_one()
    daily = (await session.execute(base.where(BroadcastItem.sent_at >= now - timedelta(days=1)))).scalar_one()
    return int(hourly), int(daily)


async def caps_reason(session, campaign: Campaign, eff: settings_store.Effective) -> str | None:
    """Причина остановки по лимиту (или None, если лимиты не выбраны)."""
    hourly_cap = campaign.hourly_cap if campaign.hourly_cap is not None else eff.hourly_cap
    daily_cap = campaign.daily_cap if campaign.daily_cap is not None else eff.daily_cap
    if not hourly_cap and not daily_cap:
        return None
    hourly, daily = await sent_counts(session, campaign.account_id)
    if hourly_cap and hourly >= hourly_cap:
        return f"достигнут часовой лимит ({hourly}/{hourly_cap})"
    if daily_cap and daily >= daily_cap:
        return f"достигнут суточный лимит ({daily}/{daily_cap})"
    return None


# --- управление прогоном ------------------------------------------------------


async def start_campaign(campaign_id: int) -> RunState:
    """Запускает (или перезапускает) рассылку кампании в фоновой задаче."""
    async with SessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            msg = f"Кампания {campaign_id} не найдена"
            raise ValueError(msg)
        if not campaign.body_template.strip():
            msg = "У кампании пустой текст сообщения"
            raise ValueError(msg)
        if campaign.status == "running" and registry.get(campaign_id) is not None:
            existing = registry.get(campaign_id)
            if existing is not None and existing.is_active:
                log.warning("Кампания %s уже выполняется — старт проигнорирован", campaign_id)
                return existing
        items, targets = await plan_items(session, campaign)
        campaign.status = "running"
        campaign.started_at = utcnow()
        campaign.finished_at = None
        await session.commit()
        total = len(items)
        queued = len([item for item in items if item.status in {"pending", "failed"}])
        targets_by_gid = {group.group_id: group for group in targets}

    run = registry.ensure(campaign_id)
    run.total = total
    run.processed = 0
    run.sent = 0
    run.failed = 0
    run.skipped = 0
    run.state = "running"
    run.current = None
    run.reason = None
    run.started_at = utcnow()
    run.finished_at = None
    run.abort_event.clear()
    run.pause_event.set()
    run.publish({"type": "started", "campaign_id": campaign_id, "total": total, "queued": queued})
    run.task = asyncio.create_task(_run(campaign_id, targets_by_gid), name=f"broadcast-{campaign_id}")
    return run


def pause_campaign(campaign_id: int) -> bool:
    """Ставит прогон на паузу (между отправками)."""
    run = registry.get(campaign_id)
    if run is None or not run.is_active:
        return False
    run.pause_event.clear()
    run.state = "paused"
    run.publish({"type": "paused", "campaign_id": campaign_id})
    return True


def resume_campaign(campaign_id: int) -> bool:
    """Снимает прогон с паузы."""
    run = registry.get(campaign_id)
    if run is None or not run.is_active:
        return False
    run.pause_event.set()
    run.state = "running"
    run.publish({"type": "resumed", "campaign_id": campaign_id})
    return True


def abort_campaign(campaign_id: int) -> bool:
    """Аварийный стоп: текущая отправка завершится, остальные не уйдут."""
    run = registry.get(campaign_id)
    if run is None or not run.is_active:
        return False
    run.abort_event.set()
    run.pause_event.set()  # если стоял на паузе — даём циклу выйти
    run.state = "aborting"
    run.publish({"type": "aborting", "campaign_id": campaign_id})
    return True


async def stop_all() -> None:
    """Останавливает все прогоны (вызывается при выключении приложения)."""
    active = registry.active()
    for run in active:
        abort_campaign(run.campaign_id)
    tasks = [run.task for run in active if run.task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Остановлено прогонов при выключении: %s", len(tasks))


# --- внутренности прогона -----------------------------------------------------


async def _interruptible_sleep(run: RunState, seconds: float) -> None:
    """Пауза, которую можно прервать кнопкой «Стоп» (не ждём всю задержку)."""
    if seconds <= 0:
        return
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        if run.abort_event.is_set():
            return
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            return
        try:
            await asyncio.wait_for(run.abort_event.wait(), timeout=min(left, 0.5))
            return  # abort взведён во время сна
        except TimeoutError:
            continue


async def _deliver(
    run: RunState,
    backend: SignalBackend,
    identifier: str,
    item: BroadcastItem,
    text: str,
    base64_attachments: list[str],
    eff: settings_store.Effective,
    is_direct: bool = False,
) -> tuple[bool, str | None]:
    """Отправка в одну группу или в личку с ретраями.

    Возвращает (успех, текст ошибки). Фатальные ошибки не ретраим: повтор
    бессмыслен и лишь увеличивает риск попасть под ограничения Signal.
    """
    max_attempts = max(1, eff.max_retries)
    for attempt in range(1, max_attempts + 1):
        item.attempts = attempt
        try:
            if is_direct:
                result = await backend.send(
                    identifier, [item.group_gid], text, base64_attachments=base64_attachments or None
                )
            else:
                result = await backend.send_group(
                    identifier, item.group_gid, text, base64_attachments=base64_attachments or None
                )
        except SignalFatalSend as exc:
            item.status = "failed"
            item.error = str(exc)
            run.publish({"type": "log", "text": f"Без ретрая {item.group_name}: {exc}"})
            return False, str(exc)
        except SignalRateLimited as exc:
            item.error = str(exc)
            wait = min(300.0, 30.0 * attempt)
            run.publish({"type": "log", "text": f"Rate limit, пауза {wait:.0f} с: {exc}"})
            await _interruptible_sleep(run, wait)
            if run.abort_event.is_set():
                item.status = "failed"
                return False, "остановлено оператором"
            continue
        except (SignalUnavailable, SignalError) as exc:
            item.error = str(exc)
            if attempt >= max_attempts:
                item.status = "failed"
                run.publish({"type": "log", "text": f"Исчерпаны попытки для {item.group_name}: {exc}"})
                return False, str(exc)
            wait = min(60.0, 2.0**attempt)
            run.publish(
                {"type": "log", "text": f"Ошибка ({attempt}/{max_attempts}) {item.group_name}, ретрай {wait:.0f} с"}
            )
            await _interruptible_sleep(run, wait)
            if run.abort_event.is_set():
                item.status = "failed"
                return False, "остановлено оператором"
            continue
        except Exception as exc:  # noqa: BLE001 - непредвиденная ошибка не должна ронять прогон
            log.exception("Непредвиденная ошибка отправки в %s", item.group_gid)
            item.status = "failed"
            item.error = f"{type(exc).__name__}: {exc}"
            return False, item.error

        item.status = "sent"
        item.signal_timestamp = result.timestamp
        item.sent_at = utcnow()
        item.error = None
        run.publish({"type": "log", "text": f"Отправлено: {item.group_name}"})
        return True, None

    item.status = "failed"
    return False, item.error or f"не удалось отправить за {max_attempts} попыток"


def _final_status(run: RunState) -> str:
    """Итоговый статус кампании по результатам прогона."""
    reason = (run.reason or "").lower()
    if "лимит" in reason:
        return "paused"
    if run.abort_event.is_set() or "остановлено" in reason:
        return "aborted"
    if run.failed and not run.sent:
        return "failed"
    return "done"


async def _mark_failed(campaign_id: int, reason: str) -> None:
    """Пишет в БД, что прогон упал (при непредвиденных исключениях)."""
    try:
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is not None:
                campaign.status = "failed"
                campaign.finished_at = utcnow()
                await session.commit()
            await audit.record(session, "campaign.crashed", f"{campaign_id}: {reason}")
    except Exception:  # noqa: BLE001 - не роняем обработку из-за журнала
        log.exception("Не удалось зафиксировать падение кампании %s", campaign_id)


async def _run(campaign_id: int, targets_by_gid: dict[str, Group]) -> None:
    """Основной цикл рассылки. Работает отдельной задачей со своей сессией БД."""
    run = registry.ensure(campaign_id)

    try:
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None:
                run.reason = f"кампания {campaign_id} не найдена"
                run.state = "finished"
                run.publish({"type": "error", "text": run.reason})
                return
            account = await session.get(Account, campaign.account_id)
            if account is None or not account.identifier:
                run.reason = "аккаунт кампании недоступен"
                run.state = "finished"
                campaign.status = "failed"
                await session.commit()
                run.publish({"type": "error", "text": run.reason})
                return

            eff = await settings_store.effective(session)
            extra_vars = json.loads(campaign.variables_json or "{}")
            attach_names = json.loads(campaign.attachments_json or "[]")
            base64_attachments = attachments.load_base64(attach_names)

            all_items = list(
                (
                    await session.execute(
                        select(BroadcastItem)
                        .where(BroadcastItem.campaign_id == campaign_id)
                        .order_by(BroadcastItem.id)
                    )
                )
                .scalars()
                .all()
            )
            pending = [item for item in all_items if item.status in {"pending", "failed"}]
            # уже отправленные учитываем в прогрессе, чтобы цифры сходились с БД
            run.sent = len([i for i in all_items if i.status == "sent"])
            run.skipped = len([i for i in all_items if i.status == "skipped"])
            run.failed = 0
            run.processed = run.sent + run.skipped
            run.publish(
                {
                    "type": "log",
                    "text": (
                        f"Старт: получателей {len(pending)}, задержка {eff.delay_min:.1f}-{eff.delay_max:.1f} с, "
                        f"вложений {len(base64_attachments)}, dry_run={campaign.dry_run}"
                    ),
                }
            )
            await _run_loop(
                run,
                session,
                campaign,
                account,
                pending,
                targets_by_gid,
                eff,
                extra_vars,
                base64_attachments,
                campaign_is_direct(campaign),
            )
    except asyncio.CancelledError:
        run.state = "finished"
        run.reason = "задача отменена"
        run.publish({"type": "done", **run.snapshot()})
        raise
    except Exception as exc:  # noqa: BLE001 - прогон не должен ронять процесс
        log.exception("Прогон кампании %s завершился ошибкой", campaign_id)
        run.state = "finished"
        run.reason = f"{type(exc).__name__}: {exc}"
        run.publish({"type": "error", "text": run.reason})
        await _mark_failed(campaign_id, run.reason)
    finally:
        registry.drop_finished()


async def _run_loop(
    run: RunState,
    session,
    campaign: Campaign,
    account: Account,
    pending: list[BroadcastItem],
    targets_by_gid: dict[str, Group],
    eff: settings_store.Effective,
    extra_vars: dict[str, str],
    base64_attachments: list[str],
    is_direct: bool,
) -> None:
    """Перебирает позиции: пауза/стоп, лимиты, рендер шаблона, отправка, задержка."""
    backend: SignalBackend = get_backend()
    last = len(pending) - 1

    for index, item in enumerate(pending):
        if run.abort_event.is_set():
            run.reason = "остановлено оператором"
            break
        await run.pause_event.wait()
        if run.abort_event.is_set():
            run.reason = "остановлено оператором"
            break

        limit_reason = await caps_reason(session, campaign, eff)
        if limit_reason:
            run.reason = limit_reason
            campaign.status = "paused"
            await session.commit()
            run.publish({"type": "log", "text": f"Пауза по лимиту: {limit_reason}"})
            break

        run.current = item.group_name or item.group_gid[:16]
        group = targets_by_gid.get(item.group_gid)
        if not is_direct and group is None:
            item.status = "failed"
            item.error = "группа недоступна (не активна или удалена)"
            item.attempts = max(item.attempts, 1)
            run.failed += 1
        else:
            text = ""
            label = (group.name if group is not None else None) or item.group_name or item.group_gid
            try:
                context = templates.build_context(group, extra_vars, account)
                if is_direct:
                    context["contact.name"] = item.group_name or item.group_gid
                    context["contact.number"] = item.group_gid
                    context.setdefault("group.name", context["contact.name"])
                    context.setdefault("group.id", item.group_gid)
                text = templates.render(campaign.body_template, context, strict=True)
            except templates.TemplateError as exc:
                item.status = "failed"
                item.error = str(exc)
                item.attempts = max(item.attempts, 1)
                run.failed += 1
                run.publish({"type": "log", "text": f"Шаблон не собран для {label}: {exc}"})

            if item.status != "failed":
                if campaign.dry_run:
                    item.status = "skipped"
                    item.error = f"dry-run: {len(text)} симв., не отправлено"
                    run.skipped += 1
                    run.publish({"type": "log", "text": f"[dry-run] {label}: {text[:60]}"})
                else:
                    ok, _error = await _deliver(
                        run, backend, account.identifier, item, text, base64_attachments, eff, is_direct
                    )
                    if ok:
                        run.sent += 1
                    else:
                        run.failed += 1

        item.updated_at = utcnow()
        run.processed += 1
        await session.commit()
        run.publish(
            {
                "type": "item",
                "id": item.id,
                "gid": item.group_gid,
                "name": item.group_name,
                "status": item.status,
                "error": item.error,
                "attempts": item.attempts,
            }
        )
        run.publish({"type": "progress", **run.snapshot()})

        # случайная задержка между группами — обязательная защита от антиспам-фильтров
        if index < last and not run.abort_event.is_set() and not campaign.dry_run:
            await _interruptible_sleep(run, random.uniform(eff.delay_min, eff.delay_max))

    run.current = None
    run.state = "finished"
    run.finished_at = utcnow()
    campaign.status = _final_status(run)
    campaign.finished_at = utcnow()
    await session.commit()
    await audit.record(session, "campaign.finished", f"{campaign.id}: {campaign.status} {run.snapshot()}")
    run.publish({"type": "done", **run.snapshot()})





