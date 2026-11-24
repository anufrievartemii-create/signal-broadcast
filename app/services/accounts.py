"""Подключение аккаунтов Signal: привязка по QR, регистрация по SMS, сверка статуса."""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, utcnow
from app.services import audit
from app.signal.base import AccountInfo, SignalBackend

log = logging.getLogger(__name__)


async def get_by_id(session: AsyncSession, account_id: int) -> Account | None:
    return await session.get(Account, account_id)


async def list_all(session: AsyncSession) -> list[Account]:
    result = await session.execute(select(Account).order_by(Account.id))
    return list(result.scalars().all())


async def list_linked(session: AsyncSession) -> list[Account]:
    """Только аккаунты, пригодные для отправки."""
    result = await session.execute(select(Account).where(Account.status == "linked").order_by(Account.id))
    return list(result.scalars().all())


async def _find_existing(session: AsyncSession, info: AccountInfo) -> Account | None:
    conditions = []
    if info.number:
        conditions.append(Account.number == info.number)
    if info.aci:
        conditions.append(Account.aci == info.aci)
    if not conditions:
        return None
    result = await session.execute(select(Account).where(or_(*conditions)))
    return result.scalars().first()


async def sync_from_backend(session: AsyncSession, backend: SignalBackend) -> list[Account]:
    """Сверяет аккаунты в БД с тем, что реально видит бэкенд Signal."""
    infos = await backend.list_accounts()
    known = await list_all(session)
    known_by_ident = {a.identifier: a for a in known if a.identifier}
    backend_idents = {i.identifier for i in infos if i.identifier}

    for info in infos:
        account = known_by_ident.get(info.identifier) or await _find_existing(session, info)
        if account is None:
            account = Account(
                number=info.number,
                aci=info.aci,
                device_name=info.device_name,
                label=info.number or (info.aci or "")[:8],
                status="linked",
                backend=backend.name,
                linked_at=utcnow(),
            )
            session.add(account)
        else:
            account.number = info.number or account.number
            account.aci = info.aci or account.aci
            account.device_name = info.device_name or account.device_name
            account.error = None
            if account.status != "linked":
                account.status = "linked"
                account.linked_at = utcnow()
        if account.identifier:
            known_by_ident[account.identifier] = account

    # аккаунты, пропавшие из бэкенда, помечаем как отвязанные
    for account in known:
        if account.identifier and account.identifier not in backend_idents and account.status == "linked":
            account.status = "unregistered"
            account.error = "Аккаунт больше не виден в бэкенде Signal"

    await session.commit()
    await audit.record(session, "accounts.sync", f"аккаунтов в бэкенде: {len(infos)}", commit=False)
    await session.commit()
    return await list_all(session)


async def start_register(
    session: AsyncSession,
    backend: SignalBackend,
    number: str,
    *,
    use_voice: bool = False,
    captcha: str | None = None,
) -> Account:
    """Запрашивает код регистрации и заводит запись аккаунта со статусом pending."""
    await backend.register(number, use_voice=use_voice, captcha=captcha)
    account = await _find_existing(session, AccountInfo(number=number))
    if account is None:
        account = Account(number=number, label=number, status="pending", backend=backend.name)
        session.add(account)
    else:
        account.status = "pending"
        account.error = None
    await session.commit()
    await audit.record(session, "accounts.register", f"номер {number}, voice={use_voice}")
    return account


async def confirm_register(
    session: AsyncSession,
    backend: SignalBackend,
    number: str,
    code: str,
    *,
    pin: str | None = None,
) -> Account:
    """Подтверждает номер кодом из SMS/звонка."""
    await backend.verify(number, code, pin=pin)
    account = await _find_existing(session, AccountInfo(number=number))
    if account is None:
        account = Account(number=number, label=number, backend=backend.name)
        session.add(account)
    account.status = "linked"
    account.linked_at = utcnow()
    account.error = None
    await session.commit()
    await audit.record(session, "accounts.verify", f"номер {number} подтверждён")
    return account


async def remove_account(
    session: AsyncSession, backend: SignalBackend, account_id: int, *, delete_remote: bool = False
) -> bool:
    """Удаляет аккаунт из панели (и, опционально, из бэкенда Signal).

    delete_remote=True стирает аккаунт на серверах Signal — необратимо.
    """
    account = await get_by_id(session, account_id)
    if account is None:
        return False
    identifier = account.identifier
    if identifier:
        try:
            await backend.unregister(identifier, delete_account=delete_remote)
        except Exception as exc:  # noqa: BLE001 - удаление из БД важнее ответа бэкенда
            log.warning("unregister %s не удался: %s", identifier, exc)
    await audit.record(
        session, "accounts.remove", f"{identifier} (remote={delete_remote})", commit=False
    )
    await session.delete(account)
    await session.commit()
    return True
