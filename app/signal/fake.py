"""FakeBackend — тестовый бэкенд. Ничего не отправляет в Signal.

Используется в unit-тестах и при SIGNAL_BACKEND=fake (демо интерфейса без аккаунта).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.signal.base import (
    AccountInfo,
    GroupInfo,
    SendResult,
    SignalError,
    SignalFatalSend,
)


@dataclass
class FakeBackend:
    """Записывает все отправки в `sent`, чтобы тесты могли их проверить."""

    name: str = "fake"
    healthy: bool = True
    accounts: list[AccountInfo] = field(default_factory=list)
    groups: dict[str, list[GroupInfo]] = field(default_factory=dict)
    sent: list[dict] = field(default_factory=list)
    #: приглашения, которые станут видны после pull_messages (имитация receive)
    staged_invites: dict[str, list[GroupInfo]] = field(default_factory=dict)
    #: приглашения, уже полученные и ещё не принятые
    pending_invites: dict[str, list[GroupInfo]] = field(default_factory=dict)
    #: группы, для которых вызывали accept_group_invite
    accepted_calls: list[tuple[str, str]] = field(default_factory=list)
    #: идентификаторы групп, отправка в которые должна падать
    failing_groups: set[str] = field(default_factory=set)
    #: идентификаторы групп с фатальной ошибкой (без ретраев)
    fatal_groups: set[str] = field(default_factory=set)
    fail_times: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.accounts:
            self.accounts = [
                AccountInfo(number="+10000000001", aci="fake-aci-1", device_name="fake-device"),
            ]
        if not self.groups:
            ident = self.accounts[0].identifier
            self.groups = {
                ident: [
                    GroupInfo(id="group.TESTGROUP1==", name="Тестовая группа", members=["+1", "+2"], is_member=True),
                    GroupInfo(id="group.TESTGROUP2==", name="Вторая группа", members=["+3"], is_member=True),
                    GroupInfo(id="group.BROKEN==", name="Сломанная группа", members=[], is_member=False),
                ]
            }

    async def health(self) -> bool:
        return self.healthy

    async def about(self) -> dict:
        return {"version": "fake", "signal_cli_version": "fake"}

    async def list_accounts(self) -> list[AccountInfo]:
        return list(self.accounts)

    def qr_link_url(self, device_name: str) -> str:
        return f"http://fake.invalid/v1/qrcodelink?device_name={device_name}"

    def receive_ws_url(self, identifier: str) -> str:
        return f"ws://fake.invalid/v1/receive/{identifier}"

    async def register(self, number: str, *, use_voice: bool = False, captcha: str | None = None) -> None:
        self.accounts.append(AccountInfo(number=number, device_name="fake-registered"))

    async def verify(self, number: str, code: str, *, pin: str | None = None) -> None:
        if code != "123-456":
            raise SignalError(f"Неверный код подтверждения: {code}")

    async def unregister(self, identifier: str, *, delete_account: bool = False) -> None:
        self.accounts = [a for a in self.accounts if a.identifier != identifier]
        self.groups.pop(identifier, None)

    async def list_groups(self, identifier: str) -> list[GroupInfo]:
        if identifier not in self.groups:
            self.groups[identifier] = []
        known = {group.id for group in self.groups[identifier]}
        pending = [group for group in self.pending_invites.get(identifier, []) if group.id not in known]
        return list(self.groups[identifier]) + pending

    async def pull_messages(self, identifier: str) -> None:
        """Переносит заранее положенные приглашения в список видимых."""
        staged = self.staged_invites.pop(identifier, [])
        if not staged:
            return
        bucket = self.pending_invites.setdefault(identifier, [])
        have = {group.id for group in bucket}
        for invite in staged:
            if invite.id not in have:
                bucket.append(invite)

    async def accept_group_invite(self, identifier: str, group_id: str) -> None:
        """Принимает приглашение, которое уже есть у этого аккаунта.

        Повторный вызов для уже принятой группы ничего не делает.
        Чужую заявку на вступление (аккаунт сам не pending) этот метод не вызывается
        из синхронизации; прямой вызов без приглашения — ошибка.
        """
        self.accepted_calls.append((identifier, group_id))
        pending = self.pending_invites.get(identifier, [])
        match = next((group for group in pending if group.id == group_id), None)
        if match is None:
            current = self.groups.get(identifier, [])
            if any(group.id == group_id and group.is_member for group in current):
                return
            raise SignalError(f"Нет ожидающего приглашения: {group_id}")
        self.pending_invites[identifier] = [group for group in pending if group.id != group_id]
        members = list(match.members)
        if identifier not in members:
            members.append(identifier)
        stored = self.groups.setdefault(identifier, [])
        existing = next((group for group in stored if group.id == group_id), None)
        if existing is not None:
            existing.is_member = True
            existing.pending_members = []
            existing.name = match.name or existing.name
            existing.members = members
            return
        stored.append(
            GroupInfo(
                id=match.id,
                name=match.name,
                description=match.description,
                members=members,
                admins=list(match.admins),
                pending_members=[],
                is_member=True,
                raw={"isMember": True},
            )
        )

    async def send(
        self,
        identifier: str,
        recipients: list[str],
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        for recipient in recipients:
            self._maybe_fail(recipient)
        self.sent.append(
            {
                "identifier": identifier,
                "recipients": list(recipients),
                "message": message,
                "attachments": list(base64_attachments or []),
                "ts": time.time(),
            }
        )
        return SendResult(timestamp=int(time.time() * 1000))

    async def send_group(
        self,
        identifier: str,
        group_id: str,
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        return await self.send(identifier, [group_id], message, base64_attachments=base64_attachments)

    async def close(self) -> None:
        return None

    # --- helpers ---------------------------------------------------------------

    def _maybe_fail(self, recipient: str) -> None:
        """Имитирует ошибки отправки для помеченных групп."""
        key = recipient
        if key in self.fatal_groups or key.removeprefix("group.") in self.fatal_groups:
            raise SignalFatalSend(f"Группа недоступна (fake): {key}")
        if key in self.failing_groups or key.removeprefix("group.") in self.failing_groups:
            left = self.fail_times.get(key, 1)
            if left > 0:
                self.fail_times[key] = left - 1
                raise SignalError(f"Временная ошибка отправки (fake), осталось {left}")
