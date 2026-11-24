"""HTML-страницы: группы, кампании, прогон, настройки (продолжение pages.py)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.routers.deps import AuthDep, SessionDep, redirect_with_message, templates
from app.services import accounts as accounts_service
from app.services import campaigns as campaigns_service
from app.services import groups as groups_service

router = APIRouter()


# --- группы ---


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(
    request: Request,
    session: SessionDep,
    account_id: int | None = None,
    search: str | None = None,
    tag: str | None = None,
    only_active: str = "false",
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    accounts = await accounts_service.list_linked(session)
    groups = await groups_service.list_groups(
        session,
        account_id=account_id,
        search=search,
        tag=tag,
        only_active=(only_active.lower() == "true"),
    )
    tags = await groups_service.all_tags(session)
    stats_ = await groups_service.group_stats(account_id)
    context = {
        "ok": ok,
        "err": err,
        "active": "groups",
        "accounts": accounts,
        "selected_account": account_id,
        "groups": groups,
        "tags": tags,
        "search": search or "",
        "tag_filter": tag or "",
        "only_active": only_active.lower() == "true",
        "stats": stats_,
    }
    return templates.TemplateResponse(request, "groups.html", context)


@router.post("/groups/bulk", response_class=RedirectResponse)
async def groups_bulk(
    session: SessionDep,
    _auth: AuthDep,
    group_ids: str = Form(default=""),
    active: str | None = Form(default=None),
    tags: str | None = Form(default=None),
) -> RedirectResponse:
    ids = [int(i) for i in group_ids.split(",") if i.strip()]
    changed_active = active is not None
    changed_active_value = active.lower() == "true"
    await groups_service.bulk_update(
        session,
        ids,
        active=changed_active_value if changed_active else None,
        tags=tags,
    )
    ok = f"Обновлено групп: {len(ids)}"
    return RedirectResponse(redirect_with_message("/groups", ok=ok), status_code=303)


# --- кампании ----------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(
    request: Request,
    session: SessionDep,
    account_id: int | None = None,
    status: str | None = None,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    accounts = await accounts_service.list_all(session)
    campaigns = await campaigns_service.list_campaigns(session, account_id=account_id, status=status)
    for campaign in campaigns:
        campaign.counts = await campaigns_service.item_counts(session, campaign.id)
    context = {
        "ok": ok,
        "err": err,
        "active": "campaigns",
        "accounts": accounts,
        "selected_account": account_id,
        "status_filter": status or "",
        "campaigns": campaigns,
    }
    return templates.TemplateResponse(request, "campaigns.html", context)


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_form_page(
    request: Request,
    session: SessionDep,
    account_id: int | None = None,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    accounts = await accounts_service.list_all(session)
    attachments_module = __import__("app.services.attachments", fromlist=["list_files"])
    attachments = attachments_module.list_files()
    context = {
        "ok": ok,
        "err": err,
        "active": "campaigns",
        "accounts": accounts,
        "selected_account": account_id,
        "attachments": attachments,
        "form": {},
        "missing_vars": [],
    }
    return templates.TemplateResponse(request, "campaign_form.html", context)
