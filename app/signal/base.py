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
    #: номера/ACI, которых пригласили, но они ещё не вступили
    pending_members: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def members_count(self) -> int:
        return len(self.members)


def _string_list(value: object) -> list[str]:
    """Список номеров из JSON: строки или объекты {number, uuid}."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("number") or item.get("uuid") or item.get("aci") or item.get("id")
            if text and str(text).strip():
                result.append(str(text).strip())
    return result


def _first_present_list(item: dict, *keys: str) -> list[str]:
    for key in keys:
        if key in item and item[key] is not None:
            return _string_list(item[key])
    return []


def group_from_payload(item: dict) -> GroupInfo | None:
    """GroupInfo из ответа listGroups / GET /v1/groups.

    REST API (bbernhard) отдаёт `member` / `pending_invites`,
    json-rpc/signal-cli — `isMember` / `pendingMembers`.
    """
    gid = item.get("id") or item.get("groupId") or ""
    if not gid:
        return None
    if "isMember" in item:
        is_member = bool(item.get("isMember"))
    elif "is_member" in item:
        is_member = bool(item.get("is_member"))
    elif "member" in item:
        is_member = bool(item.get("member"))
    else:
        is_member = True
    return GroupInfo(
        id=str(gid),
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        members=_first_present_list(item, "members"),
        admins=_first_present_list(item, "admins"),
        is_member=is_member,
        pending_members=_first_present_list(
            item,
            "pendingMembers",
            "pending_members",
            "pending_invites",
            "pendingInvites",
        ),
        raw=item,
    )


_BENIGN_ACCEPT_MARKERS = (
    "already a member",
    "already member",
    "not a pending",
    "no pending invitation",
    "no pending member",
)


def accept_already_done(exc: BaseException) -> bool:
    """Повторный приём приглашения не должен ронять синхронизацию."""
    text = str(exc).lower()
    return any(marker in text for marker in _BENIGN_ACCEPT_MARKERS)


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

    async def pull_messages(self, identifier: str) -> None:
        """Забирает входящие, чтобы приглашения в группы появились в list_groups."""

    async def receive_batch(self, identifier: str, *, timeout: float = 1.0) -> list[dict]:
        """Входящие конверты за timeout секунд. Пустой список, если ничего не пришло."""

    async def accept_group_invite(self, identifier: str, group_id: str) -> None:
        """Принимает уже существующее приглашение в группу. Повторный вызов не ошибка."""

    async def create_group(
        self,
        identifier: str,
        name: str,
        *,
        members: list[str] | None = None,
        description: str = "",
    ) -> GroupInfo:
        """Создаёт новую группу Signal. Аккаунт становится участником (и обычно админом)."""

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
