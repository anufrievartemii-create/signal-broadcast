"""RestApiBackend — клиент к bbernhard/signal-cli-rest-api (Docker-контейнер).

Проверенные эндпоинты (doc/EXAMPLES.md, src/api/api.go):
  GET    /v1/health                              -> 204, жив ли контейнер
  GET    /v1/about                               -> версии
  GET    /v1/accounts                            -> подключённые аккаунты
  GET    /v1/qrcodelink?device_name=X            -> КАРТИНКА QR (меняется на каждый запрос)
  POST   /v1/register/{number}                   -> {use_voice, captcha}
  POST   /v1/register/{number}/verify/{code}     -> {pin}
  DELETE /v1/accounts/{identifier}               -> {delete_account, delete_local_data}
  GET    /v1/groups/{identifier}                 -> список групп
  POST   /v2/send                                -> {number, recipients[], message, base64_attachments[]}
  WS     /v1/receive/{identifier}                -> входящие события
`/v1/send` в REST-обёртке помечен как deprecated — используем только /v2/send.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from app.signal.base import (
    AccountInfo,
    GroupInfo,
    SendResult,
    SignalError,
    SignalFatalSend,
    SignalNotFound,
    SignalRateLimited,
    SignalUnauthorized,
    SignalUnavailable,
    as_group_recipient,
    strip_group_prefix,
)

log = logging.getLogger(__name__)

# Маркеры ошибок, при которых повторная отправка бессмысленна
_FATAL_MARKERS = (
    "not a member",
    "group not found",
    "is blocked",
    "blocked",
    "unregistered",
    "no such account",
    "not registered",
    "invalid group",
)


def _q(value: str) -> str:
    """Экранирует идентификатор аккаунта/группы для подстановки в путь URL."""
    return quote(value, safe="")


class RestApiBackend:
    """Реализация SignalBackend поверх REST-обёртки signal-cli."""

    name = "rest_api"

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

    # --- низкоуровневый запрос -------------------------------------------------

    def _raise_for_status(self, resp: httpx.Response, method: str, path: str) -> None:
        if resp.is_success:
            return
        detail = self._error_text(resp)
        low = detail.lower()
        if resp.status_code == 429:
            raise SignalRateLimited(f"Signal ограничил частоту: {detail}")
        if resp.status_code in (401, 403):
            raise SignalUnauthorized(f"{method} {path}: {detail}")
        if resp.status_code == 404:
            raise SignalNotFound(f"{method} {path}: {detail}")
        if any(marker in low for marker in _FATAL_MARKERS):
            raise SignalFatalSend(f"{method} {path}: {detail}")
        raise SignalError(f"{method} {path} -> {resp.status_code}: {detail}")

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text.strip()[:500]
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or data).strip()[:500]
        return str(data).strip()[:500]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        try:
            resp = await self.client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            msg = f"Signal API недоступен ({self.base_url}{path}): {exc}"
            raise SignalUnavailable(msg) from exc
        self._raise_for_status(resp, method, path)
        return resp

    @staticmethod
    def _json_or_empty(resp: httpx.Response):
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("Не-JSON ответ от %s: %.200s", resp.request.url, resp.text)
            return None

    # --- состояние -------------------------------------------------------------

    async def health(self) -> bool:
        """Жив ли контейнер. GET /v1/health отдаёт 204."""
        try:
            resp = await self.client.get("/v1/health")
        except httpx.HTTPError as exc:
            log.debug("health: контейнер не отвечает: %s", exc)
            return False
        return resp.status_code in (200, 204)

    async def about(self) -> dict:
        """Версии REST-обёртки и signal-cli (удобно для диагностики)."""
        try:
            resp = await self._request("GET", "/v1/about")
        except SignalNotFound:
            return {}
        data = self._json_or_empty(resp)
        return data if isinstance(data, dict) else {"raw": data}

    async def list_accounts(self) -> list[AccountInfo]:
        """Аккаунты, подключённые к контейнеру (зарегистрированные и связанные)."""
        try:
            resp = await self._request("GET", "/v1/accounts")
        except SignalNotFound:
            return []
        data = self._json_or_empty(resp)
        if isinstance(data, dict):  # защита от варианта {"accounts": [...]}
            data = data.get("accounts") or data.get("result") or []
        accounts: list[AccountInfo] = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            accounts.append(
                AccountInfo(
                    number=item.get("number") or None,
                    aci=item.get("aci") or item.get("uuid") or None,
                    device_name=item.get("device_name") or item.get("deviceName"),
                    is_primary=item.get("is_primary"),
                    raw=item,
                )
            )
        return accounts

    def qr_link_url(self, device_name: str) -> str:
        """URL картинки QR для привязки устройства. QR меняется на каждый запрос!"""
        return f"{self.base_url}/v1/qrcodelink?device_name={_q(device_name)}"

    def receive_ws_url(self, identifier: str) -> str:
        """WebSocket входящих сообщений аккаунта."""
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/v1/receive/{_q(identifier)}"

    # --- подключение аккаунта --------------------------------------------------

    async def register(self, number: str, *, use_voice: bool = False, captcha: str | None = None) -> None:
        """Запрос кода регистрации по SMS/звонку. Может потребовать captcha."""
        payload: dict = {}
        if use_voice:
            payload["use_voice"] = True
        if captcha:
            payload["captcha"] = captcha
        await self._request("POST", f"/v1/register/{_q(number)}", json=payload or None)

    async def verify(self, number: str, code: str, *, pin: str | None = None) -> None:
        """Подтверждение номера кодом из SMS/звонка."""
        payload = {"pin": pin} if pin else None
        await self._request("POST", f"/v1/register/{_q(number)}/verify/{_q(code)}", json=payload)

    async def unregister(self, identifier: str, *, delete_account: bool = False) -> None:
        """Удаляет аккаунт из контейнера.

        delete_account=True стирает аккаунт на серверах Signal — необратимо,
        поэтому по умолчанию удаляем только локальные данные.
        """
        payload = {"delete_account": delete_account, "delete_local_data": True}
        await self._request("DELETE", f"/v1/accounts/{_q(identifier)}", json=payload)

    # --- группы ----------------------------------------------------------------

    async def list_groups(self, identifier: str) -> list[GroupInfo]:
        """Список групп аккаунта."""
        resp = await self._request("GET", f"/v1/groups/{_q(identifier)}")
        data = self._json_or_empty(resp)
        if isinstance(data, dict):
            data = data.get("groups") or data.get("result") or []
        groups: list[GroupInfo] = []
        for item in data or []:
            if not isinstance(item, dict):
                continue
            gid = item.get("id") or item.get("groupId") or ""
            if not gid:
                continue
            groups.append(
                GroupInfo(
                    id=str(gid),
                    name=str(item.get("name") or ""),
                    description=str(item.get("description") or ""),
                    members=list(item.get("members") or []),
                    admins=list(item.get("admins") or []),
                    is_member=bool(item.get("isMember", True)),
                    raw=item,
                )
            )
        return groups

    # --- отправка --------------------------------------------------------------

    async def send(
        self,
        identifier: str,
        recipients: list[str],
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        """POST /v2/send. В recipients можно смешивать номера и группы (с префиксом group.)."""
        payload: dict = {"number": identifier, "recipients": list(recipients), "message": message}
        if base64_attachments:
            payload["base64_attachments"] = list(base64_attachments)
        resp = await self._request("POST", "/v2/send", json=payload)
        data = self._json_or_empty(resp)
        timestamp = None
        if isinstance(data, dict):
            timestamp = data.get("timestamp")
        elif isinstance(data, int):
            timestamp = data
        return SendResult(timestamp=timestamp, raw=data if isinstance(data, dict) else {})

    async def send_group(
        self,
        identifier: str,
        group_id: str,
        message: str,
        *,
        base64_attachments: list[str] | None = None,
    ) -> SendResult:
        """Отправка в одну группу.

        Документация противоречива по префиксу `group.`: в EXAMPLES.md он есть
        в /v2/send, но отсутствует в пути /v1/groups/{n}/{id}. Поэтому пробуем
        основной вариант и один запасной — без префикса.
        """
        variants = [as_group_recipient(group_id)]
        stripped = strip_group_prefix(group_id)
        if stripped not in variants:
            variants.append(stripped)

        last_error: SignalError | None = None
        for variant in variants:
            try:
                return await self.send(
                    identifier, [variant], message, base64_attachments=base64_attachments
                )
            except SignalError as exc:
                last_error = exc
                log.warning("send_group: вариант %r... не сработал: %s", variant[:20], exc)
        if last_error is None:  # pragma: no cover - защита от пустого списка вариантов
            msg = f"Не удалось отправить в группу {group_id[:20]}"
            raise SignalError(msg)
        raise last_error


