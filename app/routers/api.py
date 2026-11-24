"""AJAX/API-роутеры: управление аккаунтами, группами, кампаниями, настройками, SSE-поток прогресса."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Form, HTTPException, Path, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.config import settings
from app.routers.deps import AuthDep, BackendDep, SessionDep
from app.schemas import CampaignForm, GroupBulkForm, LinkForm, RegisterForm, VerifyForm
from app.services import accounts as accounts_service
from app.services import attachments as attachments_service
from app.services import broadcaster, settings_store
from app.services import campaigns as campaigns_service
from app.services import groups as groups_service
from app.services import inbox as inbox_service
from app.signal.base import SignalError as SignalBackendError

router = APIRouter(prefix="/api")


# --- аккаунты -----------------------------------------------------------------


@router.get("/accounts")
async def api_list_accounts(session: SessionDep, backend: BackendDep, _auth: AuthDep) -> JSONResponse:
    healthy = await backend.health()
    synced = await accounts_service.sync_from_backend(session, backend) if healthy else []
    return JSONResponse(
        [
            {
                "id": a.id,
                "label": a.label,
                "number": a.number,
                "aci": a.aci,
                "device_name": a.device_name,
                "status": a.status,
                "error": a.error,
                "groups_count": len(a.groups),
                "linked_at": a.linked_at.isoformat() if a.linked_at else None,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            }
            for a in synced
        ]
    )


@router.post("/accounts/register")
async def api_register(
    session: SessionDep, backend: BackendDep, form: RegisterForm, _auth: AuthDep
) -> JSONResponse:
    if backend.name != "rest_api":
        raise HTTPException(status_code=400, detail="Регистрация доступна только в режиме rest_api")
    try:
        account = await accounts_service.start_register(
            session, backend, form.number, use_voice=form.use_voice, captcha=form.captcha
        )
    except SignalBackendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"id": account.id, "status": account.status, "number": account.number})


@router.post("/accounts/verify")
async def api_verify(
    session: SessionDep, backend: BackendDep, form: VerifyForm, _auth: AuthDep
) -> JSONResponse:
    try:
        account = await accounts_service.confirm_register(session, backend, form.number, form.code, pin=form.pin)
    except SignalBackendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"id": account.id, "status": account.status, "linked": account.linked_at is not None})


@router.post("/accounts/link")
async def api_link(
    session: SessionDep, backend: BackendDep, form: LinkForm, _auth: AuthDep
) -> JSONResponse:
    """Запуск привязки по QR: возвращает URI/URL картинки."""
    if backend.name == "rest_api":
        qr_url = backend.qr_link_url(form.device_name or settings.signal_device_name)
        return JSONResponse({"qr_url": qr_url, "device_name": form.device_name})
    uri = await backend.start_link()
    return JSONResponse({"device_link_uri": uri, "device_name": form.device_name})


@router.delete("/accounts/{account_id}")
async def api_remove_account(
    session: SessionDep, backend: BackendDep, account_id: int = Path(ge=1), _auth: AuthDep = None
) -> JSONResponse:
    ok = await accounts_service.remove_account(session, backend, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return JSONResponse({"removed": True})


@router.post("/accounts/sync")
async def api_sync_accounts(session: SessionDep, backend: BackendDep, _auth: AuthDep) -> JSONResponse:
    healthy = await backend.health()
    if not healthy:
        raise HTTPException(status_code=503, detail="Сервис Signal недоступен")
    synced = await accounts_service.sync_from_backend(session, backend)
    return JSONResponse({"synced": len(synced), "accounts": [a.identifier for a in synced]})


# --- группы -------------------------------------------------------------------


@router.post("/groups/sync")
async def api_sync_groups(
    session: SessionDep, backend: BackendDep, account_id: int = Form(...), _auth: AuthDep = None
) -> JSONResponse:
    account = await accounts_service.get_by_id(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not account.identifier:
        raise HTTPException(status_code=400, detail="Аккаунт не привязан к Signal")
    stats_ = await groups_service.sync_account_groups(session, backend, account)
    return JSONResponse({"ok": True, "summary": stats_.summary, "total": stats_.total})


@router.get("/groups")
async def api_list_groups(
    session: SessionDep,
    account_id: int | None = None,
    search: str | None = None,
    tag: str | None = None,
    only_active: bool = False,
    _auth: AuthDep = None,
) -> JSONResponse:
    groups = await groups_service.list_groups(session, account_id=account_id, search=search, tag=tag,
                                              only_active=only_active)
    tags = await groups_service.all_tags(session)
    return JSONResponse(
        {
            "groups": [
                {
                    "id": g.id,
                    "account_id": g.account_id,
                    "group_id": g.group_id,
                    "name": g.name,
                    "description": g.description,
                    "members_count": g.members_count,
                    "is_member": g.is_member,
                    "is_admin": g.is_admin,
                    "tags": g.tag_list,
                    "active": g.active,
                    "synced_at": g.synced_at.isoformat() if g.synced_at else None,
                }
                for g in groups
            ],
            "tags": tags,
            "stats": await groups_service.group_stats(session, account_id),
        }
    )


@router.post("/groups/bulk")
async def api_groups_bulk(session: SessionDep, form: GroupBulkForm, _auth: AuthDep) -> JSONResponse:
    count = await groups_service.bulk_update(session, form.group_ids, active=form.active, tags=form.tags)
    return JSONResponse({"updated": count})


# --- кампании -----------------------------------------------------------------


@router.post("/campaigns")
async def api_create_campaign(session: SessionDep, form: CampaignForm, _auth: AuthDep) -> JSONResponse:
    campaign = await campaigns_service.create(session, form)
    counts = await campaigns_service.item_counts(session, campaign.id)
    return JSONResponse({"id": campaign.id, "status": campaign.status, "groups": counts["total"]}, status_code=201)


@router.put("/campaigns/{campaign_id}")
async def api_update_campaign(
    session: SessionDep, campaign_id: int = Path(ge=1), form: CampaignForm = None, _auth: AuthDep = None
) -> JSONResponse:
    if form is None:
        raise HTTPException(status_code=400, detail="Тело запроса отсутствует")
    campaign = await campaigns_service.update(session, campaign_id, form)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return JSONResponse({"id": campaign.id, "status": campaign.status})


@router.delete("/campaigns/{campaign_id}")
async def api_delete_campaign(
    session: SessionDep, campaign_id: int = Path(ge=1), _auth: AuthDep = None
) -> JSONResponse:
    ok = await campaigns_service.delete(session, campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return JSONResponse({"deleted": True})


@router.get("/campaigns/{campaign_id}/items")
async def api_campaign_items(
    session: SessionDep,
    campaign_id: int = Path(ge=1),
    status: str | None = None,
    _auth: AuthDep = None,
) -> JSONResponse:
    counts = await campaigns_service.item_counts(session, campaign_id)
    items = await campaigns_service.list_items(session, campaign_id, status=status)
    return JSONResponse(
        {
            "counts": counts,
            "items": [
                {
                    "id": i.id,
                    "gid": i.group_gid,
                    "name": i.group_name,
                    "status": i.status,
                    "attempts": i.attempts,
                    "error": i.error,
                    "signal_timestamp": i.signal_timestamp,
                    "sent_at": i.sent_at.isoformat() if i.sent_at else None,
                }
                for i in items
            ],
        }
    )


@router.post("/campaigns/{campaign_id}/start")
async def api_start_campaign(session: SessionDep, campaign_id: int = Path(ge=1), _auth: AuthDep = None) -> JSONResponse:
    try:
        run = await broadcaster.start_campaign(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "run": run.snapshot()})


@router.post("/campaigns/{campaign_id}/pause")
async def api_pause_campaign(campaign_id: int = Path(ge=1), _auth: AuthDep = None) -> JSONResponse:
    ok = broadcaster.pause_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Прогон не найден или не активен")
    return JSONResponse({"ok": True})


@router.post("/campaigns/{campaign_id}/resume")
async def api_resume_campaign(campaign_id: int = Path(ge=1), _auth: AuthDep = None) -> JSONResponse:
    ok = broadcaster.resume_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Прогон не найден или не активен")
    return JSONResponse({"ok": True})


@router.post("/campaigns/{campaign_id}/abort")
async def api_abort_campaign(campaign_id: int = Path(ge=1), _auth: AuthDep = None) -> JSONResponse:
    ok = broadcaster.abort_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Прогон не найден или не активен")
    return JSONResponse({"ok": True})


@router.post("/campaigns/{campaign_id}/retry")
async def api_retry_failed(session: SessionDep, campaign_id: int = Path(ge=1), _auth: AuthDep = None) -> JSONResponse:
    count = await broadcaster.reset_items(session, campaign_id, statuses=("failed",))
    return JSONResponse({"reset": count})


# --- настройки ----------------------------------------------------------------


@router.get("/settings")
async def api_settings(session: SessionDep, _auth: AuthDep) -> JSONResponse:
    raw = await settings_store.get_raw(session)
    effective_ = await settings_store.effective(session)
    return JSONResponse(
        {
            "raw": raw,
            "effective": {
                "delay_min": effective_.delay_min,
                "delay_max": effective_.delay_max,
                "max_retries": effective_.max_retries,
                "hourly_cap": effective_.hourly_cap,
                "daily_cap": effective_.daily_cap,
                "auto_opt_out": effective_.auto_opt_out,
                "stop_words": effective_.stop_words,
            },
        }
    )


@router.post("/settings")
async def api_update_settings(session: SessionDep, _auth: AuthDep, **updates: object) -> JSONResponse:
    saved = await settings_store.set_values(session, dict(updates))
    return JSONResponse({"saved": saved})


# --- вложения -----------------------------------------------------------------


@router.get("/attachments")
async def api_list_attachments(_auth: AuthDep) -> JSONResponse:
    files = attachments_service.list_files()
    return JSONResponse(
        [
            {"name": f.name, "size": f.size, "size_human": f.size_human, "modified": f.modified.isoformat()}
            for f in files
        ]
    )


@router.delete("/attachments/{name}")
async def api_delete_attachment(name: str, _auth: AuthDep) -> JSONResponse:
    ok = attachments_service.delete_file(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return JSONResponse({"deleted": True})


# --- предпросмотр и валидация -------------------------------------------------


@router.post("/campaigns/preview")
async def api_preview_campaign(session: SessionDep, form: CampaignForm, _auth: AuthDep) -> JSONResponse:
    previews = await campaigns_service.preview(session, form, limit=5)
    missing = campaigns_service.missing_variables(form)
    return JSONResponse(
        {
            "previews": [
                {
                    "group_id": p.group.group_id,
                    "group_name": p.group.name,
                    "text": p.text,
                    "length": p.length,
                }
                for p in previews
            ],
            "missing_variables": missing,
        }
    )


@router.get("/campaigns/{campaign_id}/export")
async def api_export_campaign(
    session: SessionDep, campaign_id: int = Path(ge=1), _auth: AuthDep = None
) -> PlainTextResponse:
    campaign = await campaigns_service.get(session, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    items = await campaigns_service.list_items(session, campaign_id)
    content = campaigns_service.export_csv(campaign, items)
    return PlainTextResponse(content, media_type="text/csv; charset=utf-8")


# --- входящие (статус слушателя) ---------------------------------------------


@router.post("/inbox/refresh")
async def api_inbox_refresh(_auth: AuthDep) -> JSONResponse:
    await inbox_service.inbox.refresh()
    return JSONResponse({"listening": inbox_service.inbox.listening})


# --- состояние Signal ---------------------------------------------------------


@router.get("/health")
async def api_health(backend: BackendDep, _auth: AuthDep) -> JSONResponse:
    healthy = await backend.health()
    about = await backend.about() if healthy else {}
    return JSONResponse({"healthy": healthy, "backend": backend.name, "about": about})


# --- SSE-поток прогрессаCampaign ---------------------------------------------


@router.get("/stream/{campaign_id}")
async def api_stream(campaign_id: int, request: Request, _auth: AuthDep) -> StreamingResponse:
    """SSE: события прогресса рассылки клиентам, открывшим страницу прогона."""
    run = broadcaster.registry.get(campaign_id)
    if run is None:
        run = broadcaster.registry.ensure(campaign_id)

    async def event_generator():
        # отдаём историю последних событий newcomers
        for event in run.backlog:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        queue = run.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ":ping\n\n"  # hold connection
        finally:
            run.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")



