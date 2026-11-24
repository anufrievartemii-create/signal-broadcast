"""Контакты для рассылки в личные сообщения."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, OptOut
from app.schemas import ContactForm
from app.services import audit


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


async def list_contacts(
    session: AsyncSession,
    *,
    account_id: int | None = None,
    search: str | None = None,
    only_active: bool = False,
) -> list[Contact]:
    stmt = select(Contact).order_by(Contact.name, Contact.number)
    if account_id:
        stmt = stmt.where(Contact.account_id == account_id)
    if only_active:
        stmt = stmt.where(Contact.active.is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(Contact.name.ilike(pattern), Contact.number.ilike(pattern)))
    return list((await session.execute(stmt)).scalars().all())


async def create_contact(session: AsyncSession, form: ContactForm) -> Contact:
    existing = await session.execute(
        select(Contact).where(Contact.account_id == form.account_id, Contact.number == form.number)
    )
    contact = existing.scalar_one_or_none()
    if contact is None:
        contact = Contact(account_id=form.account_id, number=form.number, name=form.name.strip(), active=True)
        session.add(contact)
    else:
        if form.name.strip():
            contact.name = form.name.strip()
        contact.active = True
    await session.commit()
    await session.refresh(contact)
    await audit.record(session, "contacts.save", f"{contact.number} ({contact.name})")
    return contact


async def set_active(session: AsyncSession, contact_id: int, active: bool) -> Contact | None:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        return None
    contact.active = active
    await session.commit()
    return contact


def parse_numbers(raw: str) -> list[str]:
    """Номера из многострочного поля: +7900..., через запятую или с новой строки."""
    found: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        cleaned = "".join(ch for ch in chunk if ch.isdigit() or ch == "+").strip()
        if not cleaned:
            continue
        if not cleaned.startswith("+") and cleaned.isdigit():
            cleaned = "+" + cleaned
        if cleaned in seen:
            continue
        seen.add(cleaned)
        found.append(cleaned)
    return found


async def resolve_recipients(
    session: AsyncSession,
    *,
    account_id: int,
    numbers: list[str],
    contact_ids: list[int],
) -> list[tuple[str, str]]:
    """Уникальные получатели DM: (номер, отображаемое имя). Без отписавшихся."""
    by_number: dict[str, str] = {}
    if contact_ids:
        rows = list(
            (
                await session.execute(
                    select(Contact).where(
                        Contact.account_id == account_id,
                        Contact.id.in_(contact_ids),
                        Contact.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for contact in rows:
            by_number[contact.number] = contact.name or contact.number
    for number in numbers:
        by_number.setdefault(number, number)

    opted = await session.execute(
        select(OptOut.number).where(OptOut.account_id == account_id, OptOut.number.is_not(None))
    )
    blocked = {_digits(row[0]) for row in opted.all() if row[0]}
    result: list[tuple[str, str]] = []
    for number, name in by_number.items():
        if _digits(number) in blocked:
            continue
        result.append((number, name or number))
    result.sort(key=lambda item: item[1].lower())
    return result
