"""Pydantic-схемы: входные данные форм и API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _clean_number(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    return cleaned or None


class RegisterForm(BaseModel):
    """Запрос кода регистрации нового номера."""

    number: str = Field(min_length=5, max_length=20)
    use_voice: bool = False
    captcha: str | None = None
    label: str | None = None

    @field_validator("number")
    @classmethod
    def normalize_number(cls, value: str) -> str:
        cleaned = _clean_number(value)
        if not cleaned or not cleaned.startswith("+"):
            msg = "Номер должен быть в формате E.164, например +79001234567"
            raise ValueError(msg)
        return cleaned


class VerifyForm(BaseModel):
    """Подтверждение номера кодом из SMS/звонка."""

    number: str = Field(min_length=5, max_length=20)
    code: str = Field(min_length=3, max_length=12)
    pin: str | None = None

    @field_validator("number")
    @classmethod
    def normalize_number(cls, value: str) -> str:
        cleaned = _clean_number(value)
        if not cleaned:
            msg = "Пустой номер"
            raise ValueError(msg)
        return cleaned


class LinkForm(BaseModel):
    """Запуск привязки устройства (QR)."""

    device_name: str = Field(default="signal-broadcast", max_length=120)
    label: str | None = None


class CampaignForm(BaseModel):
    """Создание/обновление кампании."""

    name: str = Field(min_length=1, max_length=200)
    account_id: int
    body_template: str = ""
    variables: dict[str, str] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    target_mode: str = Field(default="selected", pattern="^(selected|tags|all_active|direct)$")
    group_ids: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    contact_ids: list[int] = Field(default_factory=list)
    schedule_at: datetime | None = None
    delay_min: float | None = None
    delay_max: float | None = None
    hourly_cap: int | None = None
    daily_cap: int | None = None
    dry_run: bool = False

    @field_validator("delay_min", "delay_max")
    @classmethod
    def non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value < 0:
            msg = "Задержка не может быть отрицательной"
            raise ValueError(msg)
        return min(value, 3600.0)

    @field_validator("hourly_cap", "daily_cap")
    @classmethod
    def positive_cap(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            msg = "Лимит не может быть отрицательным"
            raise ValueError(msg)
        return value or None


class GroupAddForm(BaseModel):
    """Создание группы в Signal или вступление по ID."""

    account_id: int
    action: Literal["create", "join"] = "create"
    name: str = Field(default="", max_length=255)
    group_id: str = Field(default="", max_length=255)
    members: str = Field(default="")
    description: str = Field(default="")


class ContactForm(BaseModel):
    """Новый контакт для личных сообщений."""

    account_id: int
    number: str = Field(min_length=5, max_length=32)
    name: str = Field(default="", max_length=255)

    @field_validator("number")
    @classmethod
    def normalize_contact_number(cls, value: str) -> str:
        cleaned = _clean_number(value)
        if not cleaned or not cleaned.startswith("+"):
            msg = "Номер должен быть в формате E.164, например +79001234567"
            raise ValueError(msg)
        return cleaned


class GroupBulkForm(BaseModel):
    """Массовые действия над группами."""

    group_ids: list[int] = Field(default_factory=list)
    active: bool | None = None
    tags: str | None = None


class SettingsForm(BaseModel):
    """Настройки рассылки из панели."""

    delay_min: float | None = None
    delay_max: float | None = None
    max_retries: int | None = None
    hourly_cap: int | None = None
    daily_cap: int | None = None
    stop_words: str | None = None
    auto_opt_out: bool | None = None
