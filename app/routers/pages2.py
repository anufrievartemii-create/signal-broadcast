"""HTML-страницы: группы, кампании, прогон, настройки (продолжение pages.py)."""

from __future__ import annotations

import json
from typing import assert_never

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app.routers.deps import AuthDep, BackendDep, SessionDep, redirect_with_message, templates
from app.schemas import CampaignForm, ContactForm, GroupAddForm
from app.services import accounts as accounts_service
from app.services import campaigns as campaigns_service
from app.services import contacts as contacts_service
from app.services import groups as groups_service
from app.services import settings_store
from app.signal.base import SignalError as SignalBackendError

router = APIRouter()


# --- группы ---


@router.get("/groups", response_class=HTMLResponse)
async def groups_page(
    request: Request,
    session: SessionDep,
    _auth: AuthDep,
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
    stats_ = await groups_service.group_stats(session, account_id)
    contact_rows = await contacts_service.list_contacts(session, account_id=account_id)
    context = {
        "ok": ok,
        "err": err,
        "active": "groups",
        "accounts": accounts,
        "selected_account": account_id,
        "groups": groups,
        "contacts": contact_rows,
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


@router.post("/groups/sync", response_class=RedirectResponse)
async def groups_sync(
    session: SessionDep,
    backend: BackendDep,
    _auth: AuthDep,
    account_id: int | None = Form(default=None),
) -> RedirectResponse:
    if account_id:
        account = await accounts_service.get_by_id(session, account_id)
        targets = [account] if account is not None else []
    else:
        targets = await accounts_service.list_linked(session)
    parts: list[str] = []
    for account in targets:
        if account is None or not account.identifier:
            continue
        try:
            stats_ = await groups_service.sync_account_groups(session, backend, account)
        except SignalBackendError as exc:
            return RedirectResponse(redirect_with_message("/groups", err=str(exc)), status_code=303)
        parts.append(f"{account.identifier}: {stats_.summary}")
    message = "; ".join(parts) if parts else "Нет привязанных аккаунтов для синхронизации"
    return RedirectResponse(redirect_with_message("/groups", ok=message), status_code=303)


@router.post("/groups/add", response_class=RedirectResponse)
async def groups_add(
    session: SessionDep,
    backend: BackendDep,
    _auth: AuthDep,
    account_id: int = Form(),
    action: str = Form(default="create"),
    name: str = Form(default=""),
    group_id: str = Form(default=""),
    members: str = Form(default=""),
    description: str = Form(default=""),
) -> RedirectResponse:
    try:
        form = GroupAddForm(
            account_id=account_id,
            action=action,
            name=name,
            group_id=group_id,
            members=members,
            description=description,
        )
    except ValidationError:
        return RedirectResponse(redirect_with_message("/groups", err="Проверьте поля добавления группы"), status_code=303)
    account = await accounts_service.get_by_id(session, form.account_id)
    if account is None or not account.identifier:
        return RedirectResponse(redirect_with_message("/groups", err="Выберите привязанный аккаунт"), status_code=303)
    try:
        if form.action == "create":
            group = await groups_service.create_signal_group(
                session,
                backend,
                account,
                name=form.name,
                members=groups_service.parse_member_list(form.members),
                description=form.description,
            )
            ok = f"Создана группа «{group.name}»"
        elif form.action == "join":
            group = await groups_service.join_signal_group(session, backend, account, form.group_id)
            if group is None:
                return RedirectResponse(
                    redirect_with_message(
                        "/groups",
                        err="Группа не появилась в списке. Нужно приглашение в Signal, затем «Синхронизировать».",
                    ),
                    status_code=303,
                )
            ok = f"Группа «{group.name or group.group_id}» добавлена"
        else:
            assert_never(form.action)
    except (SignalBackendError, ValueError) as exc:
        return RedirectResponse(redirect_with_message("/groups", err=str(exc)), status_code=303)
    return RedirectResponse(redirect_with_message("/groups", ok=ok), status_code=303)


@router.post("/contacts/add", response_class=RedirectResponse)
async def contacts_add(
    session: SessionDep,
    _auth: AuthDep,
    account_id: int = Form(),
    number: str = Form(),
    name: str = Form(default=""),
) -> RedirectResponse:
    try:
        form = ContactForm(account_id=account_id, number=number, name=name)
    except ValidationError:
        return RedirectResponse(
            redirect_with_message("/groups", err="Номер контакта должен быть в формате +79001234567"),
            status_code=303,
        )
    contact = await contacts_service.create_contact(session, form)
    return RedirectResponse(
        redirect_with_message("/groups", ok=f"Контакт сохранён: {contact.name or contact.number}"),
        status_code=303,
    )


# --- кампании ----------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(
    request: Request,
    session: SessionDep,
    _auth: AuthDep,
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
    _auth: AuthDep,
    account_id: int | None = None,
    ok: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    accounts = await accounts_service.list_all(session)
    groups = await groups_service.list_groups(session, account_id=account_id, only_active=True)
    tags = await groups_service.all_tags(session)
    contact_rows = await contacts_service.list_contacts(session, account_id=account_id, only_active=True)
    context = {
        "ok": ok,
        "err": err,
        "active": "campaigns",
        "accounts": accounts,
        "selected_account": account_id,
        "groups": groups,
        "contacts": contact_rows,
        "tags": tags,
        "settings": await settings_store.effective(session),
        "form": {},
        "missing_vars": [],
    }
    return templates.TemplateResponse(request, "campaign_form.html", context)


@router.post("/campaigns/new", response_class=RedirectResponse)
async def campaign_create(
    session: SessionDep,
    _auth: AuthDep,
    name: str = Form(),
    account_id: int = Form(),
    body_template: str = Form(default=""),
    variables: str = Form(default="{}"),
    target_mode: str = Form(default="selected"),
    group_ids: list[int] | None = Form(default=None),
    tags: list[str] | None = Form(default=None),
    recipients: str = Form(default=""),
    contact_ids: list[int] | None = Form(default=None),
    delay_min: float | None = Form(default=None),
    delay_max: float | None = Form(default=None),
    hourly_cap: int | None = Form(default=None),
    daily_cap: int | None = Form(default=None),
    dry_run: str | None = Form(default=None),
) -> RedirectResponse:
    try:
        parsed = json.loads(variables or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("variables")
        variables_map = {str(key): "" if value is None else str(value) for key, value in parsed.items()}
        form = CampaignForm(
            name=name,
            account_id=account_id,
            body_template=body_template,
            variables=variables_map,
            target_mode=target_mode,
            group_ids=group_ids or [],
            tags=tags or [],
            recipients=contacts_service.parse_numbers(recipients),
            contact_ids=contact_ids or [],
            delay_min=delay_min,
            delay_max=delay_max,
            hourly_cap=hourly_cap,
            daily_cap=daily_cap,
            dry_run=dry_run is not None,
        )
    except (ValidationError, ValueError, json.JSONDecodeError):
        return RedirectResponse(
            redirect_with_message("/campaigns/new", err="Проверьте название, аккаунт и JSON переменных"),
            status_code=303,
        )
    campaign = await campaigns_service.create(session, form)
    return RedirectResponse(redirect_with_message(f"/run/{campaign.id}", ok="Рассылка создана"), status_code=303)
