"""Общие зависимости роутеров: шаблоны, авторизация, сессия БД, бэкенд Signal."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.signal import get_backend
from app.signal.base import SignalBackend

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
def fmt_dt(value: datetime | str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value)[:16]


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["fmt_dt"] = fmt_dt
templates.env.globals["app_settings"] = settings
templates.env.globals["app_version"] = "0.1.0"

COOKIE_NAME = "sb_token"
LOGIN_PATH = "/login"


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


async def require_auth(request: Request) -> None:
    """Простая защита панели токеном из .env.

    Пустой ADMIN_TOKEN = авторизация выключена (локальная разработка).
    Для HTML-запросов делаем редирект на /login, для API — честный 401.
    """
    if not settings.admin_token:
        return
    if request.cookies.get(COOKIE_NAME) == settings.admin_token:
        return
    if _wants_html(request) and request.url.path != LOGIN_PATH:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": LOGIN_PATH},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется токен доступа")


SessionDep = Annotated[AsyncSession, Depends(get_session)]
BackendDep = Annotated[SignalBackend, Depends(get_backend)]
AuthDep = Annotated[None, Depends(require_auth)]


def redirect_with_message(path: str, *, ok: str | None = None, err: str | None = None) -> str:
    """Формирует путь с сообщением для пользователя (flash через query-параметр)."""
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    return f"{path}?{urlencode(params)}" if params else path
