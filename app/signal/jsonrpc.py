"""JsonRpcBackend — клиент к нативному `signal-cli daemon --http=HOST:PORT`.

Нужен, если не хочется держать Docker: ставится JRE 21+ и signal-cli, запускается
`signal-cli daemon --http=127.0.0.1:8080`. Тогда доступны:
  POST /api/v1/rpc     — одиночные и батч JSON-RPC 2.0 запросы
  GET  /api/v1/events  — SSE входящих сообщений (поддержка Last-Event-ID)
  GET  /api/v1/check   — 200, если демон жив
Мультиаккаунтность: параметр `account` внутри `params`.

Ограничение режима (man/signal-cli-jsonrpc.5.adoc): `register`, `verify` и `link`
в JSON-RPC недоступны — регистрацию номера делайте через CLI:
`signal-cli -a +7... register`. Привязка устройства работает через startLink/finishLink.
"""

from __future__ import annotations

import itertools
import logging

import httpx

from app.signal.base import (
    AccountInfo,
    GroupInfo,
    SendResult,
    SignalError,
    SignalNotFound,
    SignalRateLimited,
    SignalUnavailable,
    accept_already_done,
    group_from_payload,
)

log = logging.getLogger(__name__)
_id_counter = itertools.count(1)


class JsonRpcBackend:
    """Реализация SignalBackend поверх JSON-RPC нативного signal-cli."""

    name = "jsonrpc"

    def __init__(self, base_url: str, timeout: float = 60.0, device_name: str = "signal-broadcast") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.device_name = device_name
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @property
    def events_url(self) -> str:
        """SSE-поток входящих сообщений."""
        return f"{self.base_url}/api/v1/events"

    async def _call(self, method: str, params: dict | None = None) -> object:
        """Один JSON-RPC 2.0 запрос. Возвращает поле result."""
        payload: dict = {"jsonrpc": "2.0", "method": method, "id": next(_id_counter)}
        if params:
            payload["params"] = params
        try:
            resp = await self.client.post("/api/v1/rpc", json=payload)
        except httpx.HTTPError as exc:
            msg = f"signal-cli daemon недоступен ({self.base_url}): {exc}"
            raise SignalUnavailable(msg) from exc
        if not resp.is_success:
            raise SignalError(f"{method} -> HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        if error := data.get("error"):
            message = str(error.get("message") or error)
            low = message.lower()
            if "rate limit" in low or "429" in message:
                raise SignalRateLimited(message)
            if "not found" in low:
                raise SignalNotFound(message)
            raise SignalError(f"{method}: {message}")
        return data.get("result")

    # --- состояние -------------------------------------------------------------

    async def health(self) -> bool:
        try:
            resp = await self.client.get("/api/v1/check")
        except httpx.HTTPError as exc:
            log.debug("health: демон не отвечает: %s", exc)
            return False
        return resp.status_code == 200

    async def about(self) -> dict:
        result = await self._call("version")
        return {"version": result} if result is not None else {}

    async def list_accounts(self) -> list[AccountInfo]:
        result = await self._call("listAccounts")
        accounts: list[AccountInfo] = []
        for item in result or []:
            if not isinstance(item, dict):
                continue
            accounts.append(
                AccountInfo(
                    number=item.get("number"),
                    aci=item.get("aci") or item.get("uuid"),
                    device_name=item.get("deviceName"),
                    raw=item,
                )
            )
        return accounts

    # --- привязка устройства ---------------------------------------------------

    async def start_link(self) -> str:
        """Начинает привязку и возвращает URI вида sgnl://linkdevice?uuid=...&pub_key=...

        URI живёт недолго — показывайте его сразу (или делайте из него QR).
        """
        result = await self._call("startLink")
        uri = result.get("deviceLinkUri") if isinstance(result, dict) else None
        if not uri:
            raise SignalError("startLink не вернул deviceLinkUri")
        return str(uri)

    async def finish_link(self, device_link_uri: str, device_name: str | None = None) -> AccountInfo:
        """Завершает привязку; ждёт подтверждения с основного устройства."""
        params = {"deviceLinkUri": device_link_uri}
        if device_name:
            params["deviceName"] = device_name
        result = await self._call("finishLink", params)
        data = result if isinstance(result, dict) else {}
        return AccountInfo(number=data.get("number"), aci=data.get("aci"), device_name=device_name, raw=data)

    def qr_link_url(self, device_name: str) -> str:
        """В нативном режиме QR генерируется на стороне приложения из startLink().

        Пустая строка = показываем URI текстом или рисуем QR сами.
        """
        return ""

    def receive_ws_url(self, identifier: str) -> str:
        """WebSocket нет — входящие приходят через SSE /api/v1/events."""
        return ""

    # --- регистрация (недоступна в JSON-RPC режиме) ----------------------------

    async def register(self, number: str, *, use_voice: bool = False, captcha: str | None = None) -> None:
        msg = (
            "Регистрация номера не поддерживается в JSON-RPC режиме. Выполните в терминале: "
            f"signal-cli -a {number} register{' --voice' if use_voice else ''}, "
            f"затем signal-cli -a {number} verify CODE"
        )
        raise SignalError(msg)

    async def verify(self, number: str, code: str, *, pin: str | None = None) -> None:
        msg = (
            "Подтверждение номера не поддерживается в JSON-RPC режиме: "
            f"signal-cli -a {number} verify {code}"
        )
        raise SignalError(msg)

    async def unregister(self, identifier: str, *, delete_account: bool = False) -> None:
        msg = (
            "Удаление аккаунта в JSON-RPC режиме не реализовано — используйте CLI: "
            f"signal-cli -a {identifier} removeAccount"
        )
        raise SignalError(msg)

    # --- группы и отправка -----------------------------------------------------

    async def list_groups(self, identifier: str) -> list[GroupInfo]:
        result = await self._call("listGroups", {"account": identifier} if identifier else None)
        groups: list[GroupInfo] = []
        for item in result or []:
            if not isinstance(item, dict):
                continue
            info = group_from_payload(item)
            if info is not None:
                groups.append(info)
        return groups

    async def receive_batch(self, identifier: str, *, timeout: float = 1.0) -> list[dict]:
        """JSON-RPC receive. Пустой список, если демон уже принимает сообщения."""
        params: dict = {"timeout": timeout}
        if identifier:
            params["account"] = identifier
        try:
            result = await self._call("receive", params)
        except SignalError as exc:
            log.info("Приём сообщений %s пропущен: %s", identifier, exc)
            return []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    async def pull_messages(self, identifier: str) -> None:
        """В режиме демона приём обычно уже идёт. Короткий receive — запасной путь."""
        await self.receive_batch(identifier, timeout=1)

    async def accept_group_invite(self, identifier: str, group_id: str) -> None:
        """updateGroup принимает приглашение, если аккаунт в нём pending member."""
        params: dict = {"groupId": group_id}
        if identifier:
            params["account"] = identifier
        try:
            await self._call("updateGroup", params)
        except SignalError as exc:
            if accept_already_done(exc):
                log.info("Приглашение %s уже принято: %s", group_id[:24], exc)
                return
            raise

    async def create_group(
        self,
        identifier: str,
        name: str,
        *,
        members: list[str] | None = None,
        description: str = "",
    ) -> GroupInfo:
        params: dict = {"name": name, "members": list(members or [])}
        if identifier:
            params["account"] = identifier
        if description:
            params["description"] = description
        result = await self._call("createGroup", params)
        if isinstance(result, str) and result.strip():
            return GroupInfo(id=result.strip(), name=name, description=description, members=list(members or []), is_member=True)
        if isinstance(result, dict):
            info = group_from_payload(result)
            if info is not None:
                return info
            gid = str(result.get("id") or result.get("groupId") or "")
            if gid:
                return GroupInfo(id=gid, name=name, description=description, members=list(members or []), is_member=True)
        raise SignalError("Signal не вернул идентификатор созданной группы")

    def _send_params(
        self,
        identifier: str,
        message: str,
        base64_attachments: list[str] | None,
        **extra: object,
    ) -> dict:
        params: dict = {"message": message, **extra}
        if identifier:
            params["account"] = identifier
        if base64_attachments:
            params["base64Attachments"] = list(base64_attachments)
        return params

    async def send(
        self,
        identifier: str,
        recipients: list[str],
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        params = self._send_params(identifier, message, base64_attachments, recipient=list(recipients))
        result = await self._call("send", params)
        timestamp = result.get("timestamp") if isinstance(result, dict) else None
        return SendResult(timestamp=timestamp, raw=result if isinstance(result, dict) else {})

    async def send_group(
        self,
        identifier: str,
        group_id: str,
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        params = self._send_params(identifier, message, base64_attachments, groupId=group_id)
        result = await self._call("send", params)
        timestamp = result.get("timestamp") if isinstance(result, dict) else None
        return SendResult(timestamp=timestamp, raw=result if isinstance(result, dict) else {})


