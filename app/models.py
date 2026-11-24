"""Модели БД: аккаунты, группы, кампании, позиции рассылки, входящие, отписки, аудит."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Подключённый аккаунт Signal (связанное устройство или зарегистрированный номер)."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    number: Mapped[str | None] = mapped_column(String(32), index=True, unique=True)
    aci: Mapped[str | None] = mapped_column(String(64), index=True)
    device_name: Mapped[str | None] = mapped_column(String(120))
    # pending | linking | linked | error | unregistered
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    backend: Mapped[str] = mapped_column(String(20), default="rest_api")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    groups: Mapped[list[Group]] = relationship(
        back_populates="account", cascade="all, delete-orphan", lazy="selectin"
    )
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="account", cascade="all, delete-orphan", lazy="selectin"
    )
    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="account", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def identifier(self) -> str:
        """Идентификатор аккаунта для API: номер, иначе ACI."""
        return self.number or self.aci or ""

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Account {self.id} {self.identifier or self.label} {self.status}>"


class Group(Base):
    """Группа Signal, синхронизированная из аккаунта."""

    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("account_id", "group_id", name="uq_group_account_gid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[str] = mapped_column(String(255), index=True)  # base64 id как вернул signal-cli
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str | None] = mapped_column(Text)
    members_count: Mapped[int] = mapped_column(Integer, default=0)
    is_member: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[str] = mapped_column(String(255), default="")  # CSV
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # False = не рассылаем (отписка и т.п.)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="groups")

    @property
    def tag_list(self) -> list[str]:
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Group {self.id} {self.name!r}>"


class Contact(Base):
    """Контакт для рассылки в личные сообщения (номер Signal)."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("account_id", "number", name="uq_contact_account_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="contacts")

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Contact {self.id} {self.number}>"


class Campaign(Base):
    """Рассылка: шаблон, получатели (группы), лимиты, расписание."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    body_template: Mapped[str] = mapped_column(Text, default="")
    variables_json: Mapped[str] = mapped_column(Text, default="{}")  # доп. переменные шаблона
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")  # имена файлов в uploads_dir
    target_json: Mapped[str] = mapped_column(Text, default='{"mode": "selected", "group_ids": []}')
    # draft | scheduled | running | paused | done | aborted | failed
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)

    schedule_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delay_min: Mapped[float] = mapped_column(Float, default=3.0)
    delay_max: Mapped[float] = mapped_column(Float, default=8.0)
    hourly_cap: Mapped[int | None] = mapped_column(Integer)
    daily_cap: Mapped[int | None] = mapped_column(Integer)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="campaigns")
    items: Mapped[list[BroadcastItem]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", lazy="selectin"
    )


class BroadcastItem(Base):
    """Одна отправка = одна группа внутри кампании. Идемпотентность по (campaign, group_gid)."""

    __tablename__ = "broadcast_items"
    __table_args__ = (UniqueConstraint("campaign_id", "group_gid", name="uq_item_campaign_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    group_gid: Mapped[str] = mapped_column(String(255), index=True)
    group_name: Mapped[str] = mapped_column(String(255), default="")  # снапшот на момент рассылки
    # pending | sent | failed | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    signal_timestamp: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="items")


class InboxEvent(Base):
    """Входящее событие из Signal (сообщение, набор текста, квитанция)."""

    __tablename__ = "inbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), default="message")
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    source_name: Mapped[str | None] = mapped_column(String(255))
    group_gid: Mapped[str | None] = mapped_column(String(255), index=True)
    message: Mapped[str | None] = mapped_column(Text)
    signal_timestamp: Mapped[int | None] = mapped_column(Integer)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OptOut(Base):
    """Отписка: группа или номер, куда больше не рассылаем."""

    __tablename__ = "opt_outs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    group_gid: Mapped[str | None] = mapped_column(String(255), index=True)
    number: Mapped[str | None] = mapped_column(String(64), index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Настройки, меняемые из панели (перекрывают значения из .env)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Журнал действий: что, когда и с каким результатом."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str | None] = mapped_column(Text)


