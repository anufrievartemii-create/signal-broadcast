"""Абстракция бэкенда Signal.

Единый интерфейс, чтобы приложение не зависело от способа запуска signal-cli:
- RestApiBackend  -> контейнер bbernhard/signal-cli-rest-api (по умолчанию)
- JsonRpcBackend  -> нативный `signal-cli daemon --http` (нужен JRE 21+)
- FakeBackend     -> тесты и демонстрация, ничего не отправляет
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

GROUP_PREFIX = "group."


class SignalError(Exception):
    """Базовая ошибка взаимодействия с Signal."""


class SignalUnavailable(SignalError):
    """Бэкенд недоступен (контейнер не поднят, сеть, таймаут)."""


class SignalNotFound(SignalError):
    """Аккаунт/группа не найдены."""


class SignalUnauthorized(SignalError):
    """Нет прав / аккаунт не зарегистрирован в бэкенде."""


class SignalRateLimited(SignalError):
    """Signal ограничил частоту отправок — нужна пауза, не ретрай в лоб."""


class SignalFatalSend(SignalError):
    """Фатальная ошибка отправки (не участник группы, группа заблокирована).

    Ретраи бессмысленны, позицию помечаем failed сразу.
    """


def as_group_recipient(group_id: str) -> str:
    """В /v2/send группы передаются с префиксом `group.` (см. doc/EXAMPLES.md)."""
    return group_id if group_id.startswith(GROUP_PREFIX) else GROUP_PREFIX + group_id


def strip_group_prefix(group_id: str) -> str:
    """Обратная операция: для путей вида /v1/groups/{number}/{group_id}."""
    return group_id[len(GROUP_PREFIX) :] if group_id.startswith(GROUP_PREFIX) else group_id


@dataclass(slots=True)
class AccountInfo:
    """Аккаунт, известный бэкенду Signal."""

    number: str | None = None
    aci: str | None = None
    device_name: str | None = None
    is_primary: bool | None = None
    raw: dict = field(default_factory=dict)

    @property
    def identifier(self) -> str:
        return self.number or self.aci or ""


@dataclass(slots=True)
class GroupInfo:
    """Группа Signal."""

    id: str  # base64 id ровно как вернул signal-cli
    name: str = ""
    description: str = ""
    members: list[str] = field(default_factory=list)
    admins: list[str] = field(default_factory=list)
    is_member: bool = True
    raw: dict = field(default_factory=dict)

    @property
    def members_count(self) -> int:
        return len(self.members)


@dataclass(slots=True)
class SendResult:
    """Результат отправки."""

    timestamp: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class IncomingEvent:
    """Входящее событие (сообщение/набор текста/квитанция)."""

    kind: str = "message"
    source: str | None = None
    source_name: str | None = None
    group_gid: str | None = None
    message: str | None = None
    timestamp: int | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class SignalBackend(Protocol):
    """Контракт бэкенда Signal. Все методы асинхронные."""

    name: str

    async def health(self) -> bool: ...

    async def about(self) -> dict: ...

    async def list_accounts(self) -> list[AccountInfo]: ...

    def qr_link_url(self, device_name: str) -> str: ...

    async def register(self, number: str, *, use_voice: bool = False, captcha: str | None = None) -> None: ...

    async def verify(self, number: str, code: str, *, pin: str | None = None) -> None: ...

    async def unregister(self, identifier: str, *, delete_account: bool = False) -> None: ...

    async def list_groups(self, identifier: str) -> list[GroupInfo]: ...

    async def send(
        self,
        identifier: str,
        recipients: list[str],
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult: ...

    async def send_group(
        self, identifier: str, group_id: str, message: str, *, base64_attachments: list[str] | None = None
    ) -> SendResult: ...

    def receive_ws_url(self, identifier: str) -> str: ...

    async def close(self) -> None: ...
