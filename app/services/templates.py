"""Рендеринг шаблонов сообщений.

Синтаксис: `{{group.name}}`, `{{date}}`, `{{my_var}}` — простые подстановки без
выполнения кода. Неизвестная переменная = ошибка с понятным списком, чтобы
рассылка не ушла с мусором вместо текста.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.models import Account, Group

VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


class TemplateError(ValueError):
    """В шаблоне есть переменные, которых нет в контексте."""

    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(slots=True)
class Preview:
    """Предпросмотр одной группы."""

    group: Group
    text: str
    length: int


def extract_variables(text: str) -> list[str]:
    """Все переменные шаблона, отсортированные и без повторов."""
    return sorted(set(VAR_RE.findall(text or "")))


def build_context(
    group: Group | None = None,
    extra_vars: dict[str, str] | None = None,
    account: Account | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Контекст подстановки для одной группы."""
    now = now or datetime.now()
    context: dict[str, str] = {
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%d.%m.%Y %H:%M"),
        "year": now.strftime("%Y"),
        "month": now.strftime("%m"),
        "day": now.strftime("%d"),
    }
    if group is not None:
        context["group.name"] = group.name or ""
        context["group.id"] = group.group_id
        context["group.description"] = group.description or ""
        context["group.members"] = str(group.members_count)
        context["group.tags"] = group.tags or ""
    if account is not None:
        context["account.label"] = account.label or ""
        context["account.number"] = account.identifier
    for key, value in (extra_vars or {}).items():
        context[str(key)] = "" if value is None else str(value)
    return context


def render(text: str, context: dict[str, str], *, strict: bool = True) -> str:
    """Подставляет значения в шаблон.

    strict=True: при неизвестной переменной бросает TemplateError (используется
    перед реальной отправкой). strict=False: оставляет `{{var}}` как есть — для
    черновиков и предпросмотра в форме.
    """
    text = text or ""
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return str(context[key])
        missing.append(key)
        return match.group(0)

    rendered = VAR_RE.sub(repl, text)
    if missing and strict:
        uniq = sorted(set(missing))
        raise TemplateError(f"Не заданы переменные шаблона: {', '.join(uniq)}", uniq)
    return rendered


def preview_for_groups(
    text: str,
    groups: list[Group],
    extra_vars: dict[str, str] | None = None,
    account: Account | None = None,
    limit: int = 5,
) -> list[Preview]:
    """Предпросмотр первых `limit` групп (для формы кампании)."""
    previews: list[Preview] = []
    for group in groups[:limit]:
        context = build_context(group, extra_vars, account)
        rendered = render(text, context, strict=False)
        previews.append(Preview(group=group, text=rendered, length=len(rendered)))
    return previews


def check_variables(text: str, extra_vars: dict[str, str] | None = None) -> list[str]:
    """Переменные шаблона, которые НЕ покрываются автоматически и не заданы вручную."""
    known_prefixes = ("date", "time", "datetime", "year", "month", "day", "group.", "account.")
    manual = set(extra_vars or {})
    return [
        var
        for var in extract_variables(text)
        if var not in manual and not var.startswith(known_prefixes)
    ]
