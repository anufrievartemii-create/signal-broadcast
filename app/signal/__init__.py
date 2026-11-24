"""Пакет бэкендов Signal."""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.signal.base import (
    AccountInfo,
    GroupInfo,
    IncomingEvent,
    SendResult,
    SignalBackend,
    SignalError,
    SignalFatalSend,
    SignalNotFound,
    SignalRateLimited,
    SignalUnauthorized,
    SignalUnavailable,
    as_group_recipient,
    strip_group_prefix,
)


@lru_cache
def get_backend() -> SignalBackend:
    """Фабрика бэкенда по настройке SIGNAL_BACKEND."""
    kind = settings.signal_backend.lower().strip()
    if kind == "rest_api":
        from app.signal.rest_api import RestApiBackend

        return RestApiBackend(
            base_url=settings.signal_api_url,
            timeout=settings.signal_api_timeout,
            device_name=settings.signal_device_name,
        )
    if kind == "jsonrpc":
        from app.signal.jsonrpc import JsonRpcBackend

        return JsonRpcBackend(
            base_url=settings.signal_api_url,
            timeout=settings.signal_api_timeout,
            device_name=settings.signal_device_name,
        )
    if kind == "fake":
        from app.signal.fake import FakeBackend

        return FakeBackend()
    msg = f"Неизвестный SIGNAL_BACKEND: {settings.signal_backend!r} (ожидается rest_api | jsonrpc | fake)"
    raise ValueError(msg)


def reset_backend_cache() -> None:
    """Сбрасывает кэш фабрики (используется в тестах)."""
    get_backend.cache_clear()


__all__ = [
    "AccountInfo",
    "GroupInfo",
    "IncomingEvent",
    "SendResult",
    "SignalBackend",
    "SignalError",
    "SignalFatalSend",
    "SignalNotFound",
    "SignalRateLimited",
    "SignalUnauthorized",
    "SignalUnavailable",
    "as_group_recipient",
    "get_backend",
    "reset_backend_cache",
    "strip_group_prefix",
]
