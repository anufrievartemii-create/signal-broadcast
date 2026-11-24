import asyncio
import json

import pytest

from app.config import settings
from app.db import SessionLocal, engine, init_db, dispose_engine
from app.models import Base
from app.signal import reset_backend_cache
from app.signal.base import SignalBackend
from app.signal.fake import FakeBackend
from app.services import broadcaster
from app.services import accounts as accounts_service
from app.services import campaigns as campaigns_service
from app.services import groups as groups_service
from app.services import settings_store
from app.services.templates import render, build_context, extract_variables, TemplateError
from app.schemas import CampaignForm


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="function", autouse=True)
async def clean_db():
    """Свежая БД на каждый тест (удалём все строки)."""
    await init_db()
    async with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def use_fake_backend():
    """Подменяет бэкенд Signal на FakeBackend для всех тестов."""
    settings.signal_backend = "fake"
    reset_backend_cache()
    yield
    settings.signal_backend = "rest_api"
    reset_backend_cache()


@pytest.fixture
def fake_backend():
    return FakeBackend()


def test_render_variables():
    g = type("G", (), {"name": "Клуб", "group_id": "gid", "description": "", "members_count": 2, "tags": ""})()
    ctx = build_context(g)
    out = render("Привет, {{group.name}}! Дата {{date}}.", ctx)
    assert "Привет, Клуб!" in out
    assert ctx["date"]  # дата подставлена автоматически


def test_render_missing_strict():
    text = "Пока {{unknown.var}}"
    with pytest.raises(TemplateError) as exc:
        render(text, {}, strict=True)
    assert "unknown.var" in exc.value.missing


def test_extract_variables():
    assert extract_variables("{{a}} {{b}} {{a}}") == ["a", "b"]


@pytest.mark.asyncio
async def test_plan_and_send(fake_backend):
    from app.models import Account, Campaign, Group
    async with SessionLocal() as session:
        acc = Account(number="+10000000001", status="linked", backend="fake")
        session.add(acc)
        await session.commit()
        camp = Campaign(name="Тест", account_id=acc.id, body_template="Привет {{group.name}}", status="draft",
                        target_json='{"mode": "all_active", "group_ids": [], "tags": []}')
        session.add(camp)
        await session.commit()
        await session.refresh(camp)

        group = Group(account_id=acc.id, group_id="group.TEST==", name="Тестовая группа", active=True, is_member=True)
        session.add(group)
        await session.commit()

        items, targets = await broadcaster.plan_items(session, camp)
        assert len(items) == 1
        assert targets[0].group_id == "group.TEST=="

        run = await broadcaster.start_campaign(camp.id)
        # даём поработать
        await asyncio.sleep(2)
        broadcaster.abort_campaign(camp.id)
        await asyncio.sleep(0.5)

        async with SessionLocal() as s2:
            camp2 = await s2.get(Campaign, camp.id)
            print("STATUS:", camp2.status)
            items2 = await campaigns_service.list_items(s2, camp.id)
            for it in items2:
                print("ITEM:", it.group_gid, it.status, it.error, it.attempts)
            assert any(it.status == "sent" for it in items2), "ожидалась хотя бы одна отправка"


@pytest.mark.asyncio
async def test_pause_resume(fake_backend):
    from app.models import Account, Campaign, Group
    async with SessionLocal() as session:
        acc = Account(number="+10000000001", status="linked", backend="fake")
        session.add(acc)
        await session.commit()
        camp = Campaign(name="Пауза", account_id=acc.id, body_template="x", status="draft",
                        target_json='{"mode": "all_active", "group_ids": [], "tags": []}')
        session.add(camp)
        await session.commit()
        for gid in ("group.P1==", "group.P2=="):
            session.add(Group(account_id=acc.id, group_id=gid, name=f"G{gid[:6]}", active=True, is_member=True))
        await session.commit()
        cid = camp.id

    async with SessionLocal() as s:
        await settings_store.set_values(s, {"delay_min": "2", "delay_max": "3"})

    run = await broadcaster.start_campaign(cid)
    await asyncio.sleep(1)  # первая группа ушла мгновенно, вторая в задержке 2-3 с
    assert broadcaster.pause_campaign(cid)
    run2 = broadcaster.registry.get(cid)
    assert run2.state == "paused"
    assert run2.state == "paused"
    async with SessionLocal() as s2:
        items = await campaigns_service.list_items(s2, cid)
        statuses = {it.group_gid: it.status for it in items}
        assert statuses.get("group.P1==") == "sent"
    assert broadcaster.resume_campaign(cid)
    await asyncio.sleep(4.5)  # достаточно для задержки 2-3 с и отправки второй группы
    broadcaster.abort_campaign(cid)
    await asyncio.sleep(0.3)
    fin = broadcaster.registry.get(cid)
    assert fin.state in ("finished", "aborted")
    async with SessionLocal() as s3:
        items = await campaigns_service.list_items(s3, cid)
        statuses = {it.group_gid: it.status for it in items}
        assert statuses.get("group.P1==") == "sent"
        assert statuses.get("group.P2==") == "sent"

