"""Слушатель входящих: WebSocket /v1/receive/{number} и обработка стоп-слов.

Зачем нужен:
1. Отписки по ключевому слову («стоп») — группа снимается с рассылки.
2. Signal требует регулярного приёма сообщений, иначе составы групп и таймеры
   исчезновения устаревают. Поэтому контейнер держим с AUTO_RECEIVE_SCHEDULE,
   а приложение — с постоянным WebSocket-подключением.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import websockets
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, Group, InboxEvent, OptOut
from app.services import audit, settings_store
from app.services import groups as groups_service
from app.signal import get_backend
from app.signal.base import SignalBackend, SignalError

log = logging.getLogger(__name__)

_RECONNECT_MIN = 5.0
_RECONNECT_MAX = 120.0


def contains_stop_word(text: str, stop_words: list[str]) -> str | None:
    """Возвращает найденное стоп-слово или None.

    Ищем отдельным словом, чтобы «стоп-кран» не считался отпиской.
    """
    if not text or not stop_words:
        return None
    cleaned = text.strip().lower().rstrip(".!?").strip()
    words = re.findall(r"[\w\-]+", cleaned, flags=re.UNICODE)
    for word in stop_words:
        if cleaned == word or word in words:
            return word
    return None


def is_group_membership_event(data: dict) -> bool:
    """Событие про состав группы, а не обычное сообщение в чат.

    Приглашение приходит как groupV2 без текста. Текстовые сообщения групп
    не запускают синхронизацию на каждое слово.
    """
    envelope = data.get("envelope") if isinstance(data, dict) else None
    if not isinstance(envelope, dict):
        return False
    data_message = envelope.get("dataMessage")
    if isinstance(data_message, dict):
        has_group = bool(
            data_message.get("groupV2") or data_message.get("groupInfo") or data_message.get("groupContext")
        )
        if has_group and not data_message.get("message"):
            return True
    sync = envelope.get("syncMessage")
    if isinstance(sync, dict):
        sent = sync.get("sentMessage") if isinstance(sync.get("sentMessage"), dict) else {}
        has_group = bool(sent.get("groupV2") or sent.get("groupInfo") or sent.get("groupContext"))
        if has_group and not sent.get("message"):
            return True
    return False


def parse_envelope(data: dict) -> dict:
    """Достаёт из конверта нужные поля (терпимо к разным версиям формата)."""
    envelope = data.get("envelope") if isinstance(data, dict) else None
    if not isinstance(envelope, dict):
        return {}
    parsed: dict = {
        "kind": "message",
        "source": envelope.get("source") or envelope.get("sourceNumber") or envelope.get("sourceUuid"),
        "source_name": envelope.get("sourceName"),
        "timestamp": envelope.get("timestamp"),
        "message": None,
        "group_gid": None,
    }
    data_message = envelope.get("dataMessage")
    if isinstance(data_message, dict):
        parsed["message"] = data_message.get("message")
        group_info = data_message.get("groupInfo") or data_message.get("groupContext") or {}
        if isinstance(group_info, dict):
            parsed["group_gid"] = group_info.get("groupId") or group_info.get("id")
    elif envelope.get("typingMessage"):
        parsed["kind"] = "typing"
    elif envelope.get("receiptMessage"):
        parsed["kind"] = "receipt"
    elif envelope.get("storyMessage"):
        parsed["kind"] = "story"
    sync = envelope.get("syncMessage")
    if isinstance(sync, dict) and not parsed["message"]:
        sent = sync.get("sentMessage") or {}
        parsed["kind"] = "sync"
        parsed["message"] = sent.get("message")
    return parsed


class InboxListener:
    """Держит по одному WebSocket-подключению на каждый привязанный аккаунт."""

    def __init__(self, backend: SignalBackend | None = None) -> None:
        self._backend = backend
        self._tasks: dict[str, asyncio.Task] = {}
        self._group_sync_tasks: dict[int, asyncio.Task] = {}
        self._stop = asyncio.Event()

    @property
    def backend(self) -> SignalBackend:
        return self._backend or get_backend()

    @property
    def listening(self) -> list[str]:
        """Идентификаторы аккаунтов с активным слушателем."""
        return sorted(self._tasks)

    async def start(self) -> None:
        """Запускает слушателей (вызывается из lifespan)."""
        self._stop.clear()
        await self.refresh()

    async def refresh(self) -> None:
        """Перечитывает аккаунты и поднимает/гасит слушатели под текущий список."""
        async with SessionLocal() as session:
            result = await session.execute(select(Account).where(Account.status == "linked"))
            accounts = list(result.scalars().all())

        wanted: dict[str, tuple[int, str]] = {}
        for account in accounts:
            identifier = account.identifier
            if not identifier:
                continue
            url = self.backend.receive_ws_url(identifier)
            if not url:
                log.warning(
                    "Бэкенд %s не даёт WebSocket входящих — слушатель для %s не запущен",
                    self.backend.name,
                    identifier,
                )
                continue
            wanted[identifier] = (account.id, url)

        for identifier in [k for k in self._tasks if k not in wanted]:
            task = self._tasks.pop(identifier)
            task.cancel()
            log.info("Слушатель %s остановлен (аккаунт отвязан)", identifier)

        for identifier, (account_id, url) in wanted.items():
            if identifier not in self._tasks:
                self._tasks[identifier] = asyncio.create_task(
                    self._listen(identifier, account_id, url), name=f"inbox-{identifier}"
                )

    async def stop(self) -> None:
        """Останавливает все подключения (выключение приложения)."""
        self._stop.set()
        tasks = list(self._tasks.values()) + list(self._group_sync_tasks.values())
        self._tasks.clear()
        self._group_sync_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Слушатель входящих остановлен")

    async def _listen(self, identifier: str, account_id: int, url: str) -> None:
        """Цикл подключения с экспоненциальным backoff после обрывов."""
        backoff = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=60) as ws:
                    log.info("Слушаю входящие %s", identifier)
                    backoff = _RECONNECT_MIN
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._on_message(account_id, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - переподключаемся при любой ошибке сети
                log.warning("WebSocket %s оборвался: %s", identifier, exc)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)

    async def _on_message(self, account_id: int, raw: str | bytes) -> None:
        """Разбирает одно входящее событие, сохраняет и проверяет стоп-слова."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("Входящее событие не JSON, пропускаю")
            return
        parsed = parse_envelope(data)
        if not parsed:
            return
        async with SessionLocal() as session:
            eff = await settings_store.effective(session)
            message = parsed.get("message")
            event = InboxEvent(
                account_id=account_id,
                kind=str(parsed.get("kind") or "message"),
                source=parsed.get("source"),
                source_name=parsed.get("source_name"),
                group_gid=parsed.get("group_gid"),
                message=message,
                signal_timestamp=parsed.get("timestamp"),
            )
            session.add(event)
            stop_word = contains_stop_word(message or "", eff.stop_words) if eff.auto_opt_out else None
            if stop_word:
                await self._apply_opt_out(session, account_id, parsed, stop_word)
                event.handled = True
            await session.commit()
        if is_group_membership_event(data):
            self._schedule_group_sync(account_id)

    def _schedule_group_sync(self, account_id: int) -> None:
        """Один синк на аккаунт, пока предыдущий не закончился."""
        current = self._group_sync_tasks.get(account_id)
        if current is not None and not current.done():
            return
        self._group_sync_tasks[account_id] = asyncio.create_task(
            self._sync_after_invite(account_id),
            name=f"invite-sync-{account_id}",
        )

    async def _sync_after_invite(self, account_id: int) -> None:
        """Приглашение уже пришло по сокету — принимать без второго receive."""
        try:
            await asyncio.sleep(1.0)
            if self._stop.is_set():
                return
            async with SessionLocal() as session:
                account = await session.get(Account, account_id)
                if account is None or account.status != "linked" or not account.identifier:
                    return
                await groups_service.sync_account_groups(
                    session,
                    self.backend,
                    account,
                    pull_messages=False,
                )
        except asyncio.CancelledError:
            raise
        except SignalError as exc:
            log.warning("Не удалось принять приглашение аккаунта %s: %s", account_id, exc)
        except Exception:
            log.exception("Не удалось синхронизировать группы после входящего события")

    async def _apply_opt_out(self, session, account_id: int, parsed: dict, word: str) -> None:
        """Фиксирует отписку: группа снимается с рассылок."""
        group_gid = parsed.get("group_gid")
        source = parsed.get("source")
        session.add(
            OptOut(
                account_id=account_id,
                group_gid=group_gid,
                number=None if group_gid else source,
                reason=f"стоп-слово {word!r}",
            )
        )
        if group_gid:
            result = await session.execute(
                select(Group).where(Group.account_id == account_id, Group.group_id == group_gid)
            )
            for group in result.scalars().all():
                group.active = False
        await audit.record(
            session,
            "inbox.opt_out",
            f"слово {word!r} от {source}, группа={'да' if group_gid else 'нет'}",
            commit=False,
        )
        log.warning("Отписка по стоп-слову %r: %s", word, group_gid or source)


#: единственный слушатель на приложение
inbox = InboxListener()

