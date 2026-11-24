"""HTML-страницы панели (Jinja2 + htmx)."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.routers.deps import COOKIE_NAME, AuthDep, BackendDep, SessionDep, templates
from app.services import accounts as accounts_service
from app.services import audit, broadcaster, settings_store
from app.services import campaigns as campaigns_service
from app.services import groups as groups_service

router = APIRouter()


# --- авторизация --------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, err: str | None = None) -> HTMLResponse:
    auth_required = bool(settings.admin_token)
    return templates.TemplateResponse(
        request, "login.html", {"error": err, "auth_required": auth_required}
    )


@router.post("/login")
async def login_submit(request: Request, token: str = Form(default="")) -> RedirectResponse:
    if not settings.admin_token or token == settings.admin_token:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=86400 * 7)
        return response
    return RedirectResponse(f"/login?{urlencode({'err': 'Неверный токен'})}", status_code=303)


@router.get("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- дашборд ------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: SessionDep,
    backend: BackendDep,
    _auth: AuthDep,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    healthy = await backend.health()
    about = await backend.about() if healthy else {}
    accounts = await accounts_service.list_all(session)
    linked = [a for a in accounts if a.status == "linked"]

    hourly = daily = 0
    for account in linked:
        account_hourly, account_daily = await broadcaster.sent_counts(session, account.id)
        hourly += account_hourly
        daily += account_daily

    context = {
        "ok": ok,
        "err": err,
        "active": "dashboard",
        "healthy": healthy,
        "about": about,
        "backend_name": backend.name,
        "accounts": accounts,
        "linked_count": len(linked),
        "group_stats": await groups_service.group_stats(session),
        "campaigns": await campaigns_service.list_campaigns(session),
        "runs": [run.snapshot() for run in broadcaster.registry.all()],
        "hourly_sent": hourly,
        "daily_sent": daily,
        "settings": await settings_store.effective(session),
        "logs": await audit.recent(session, limit=15),
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


# --- аккаунты -----------------------------------------------------------------


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    session: SessionDep,
    backend: BackendDep,
    _auth: AuthDep,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    healthy = await backend.health()
    accounts = await accounts_service.list_all(session)
    context = {
        "ok": ok,
        "err": err,
        "active": "accounts",
        "healthy": healthy,
        "backend_name": backend.name,
        "accounts": accounts,
        "qr_url": backend.qr_link_url(settings.signal_device_name),
        "device_name": settings.signal_device_name,
        "can_register": backend.name == "rest_api",
        "counts": {a.id: len(a.groups) for a in accounts},
    }
    return templates.TemplateResponse(request, "accounts.html", context)


# --- страница прогона ---------------------------------------------------------


@router.get("/run/{campaign_id}", response_class=HTMLResponse)
async def run_page(
    request: Request,
    session: SessionDep,
    _auth: AuthDep,
    campaign_id: int,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    campaign = await campaigns_service.get(session, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    items = await campaigns_service.list_items(session, campaign_id)
    run = broadcaster.registry.get(campaign_id)
    context = {
        "ok": ok,
        "err": err,
        "active": "campaigns",
        "campaign": campaign,
        "items": items,
        "run": run.snapshot() if run else {"state": "idle", "total": 0, "sent": 0, "failed": 0, "percent": 0},
    }
    return templates.TemplateResponse(request, "run.html", context)

