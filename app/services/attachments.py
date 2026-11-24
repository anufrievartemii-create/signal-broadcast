"""Вложения кампаний: сохранение загруженных файлов и чтение их в base64.

REST-обёртка принимает вложения как base64 в теле /v2/send, поэтому файлы лежат
локально в data/uploads и читаются один раз на кампанию.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 МБ: base64 раздувает тело запроса на ~33%
_UNSAFE = re.compile(r"[^A-Za-z0-9._@+-]")


class AttachmentError(ValueError):
    """Проблема с вложением: слишком большое, не найдено, недопустимое имя."""


@dataclass(slots=True)
class StoredFile:
    name: str
    size: int
    modified: datetime

    @property
    def size_human(self) -> str:
        size = float(self.size)
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if size < 1024 or unit == "ГБ":
                return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} ГБ"


def sanitize_name(filename: str) -> str:
    """Убирает пути и недопустимые символы из имени загруженного файла."""
    name = Path(filename or "").name.strip()
    name = _UNSAFE.sub("_", name)
    if not name or name in {".", ".."}:
        raise AttachmentError("Недопустимое имя файла")
    return name[:120]


def uploads_dir() -> Path:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    return settings.uploads_dir


async def save_upload(filename: str, content: bytes) -> StoredFile:
    """Сохраняет байты в data/uploads и возвращает описание файла."""
    if not content:
        raise AttachmentError("Пустой файл")
    if len(content) > MAX_ATTACHMENT_BYTES:
        msg = f"Файл больше {MAX_ATTACHMENT_BYTES // (1024 * 1024)} МБ"
        raise AttachmentError(msg)
    name = sanitize_name(filename)
    path = uploads_dir() / name
    path.write_bytes(content)
    stat = path.stat()
    return StoredFile(name=name, size=stat.st_size, modified=datetime.fromtimestamp(stat.st_mtime))


def list_files() -> list[StoredFile]:
    """Все сохранённые вложения, новые сверху."""
    files: list[StoredFile] = []
    for path in uploads_dir().iterdir():
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(StoredFile(name=path.name, size=stat.st_size, modified=datetime.fromtimestamp(stat.st_mtime)))
    return sorted(files, key=lambda f: f.modified, reverse=True)


def delete_file(name: str) -> bool:
    """Удаляет вложение по имени (без выхода за пределы data/uploads)."""
    path = uploads_dir() / sanitize_name(name)
    if path.is_file():
        path.unlink()
        return True
    return False


def load_base64(names: list[str]) -> list[str]:
    """Читает перечисленные файлы и возвращает base64-строки для /v2/send.

    Отсутствующие файлы пропускаются с предупреждением: рассылка не должна
    падать из-за одного удалённого вложения.
    """
    encoded: list[str] = []
    base = uploads_dir()
    for name in names or []:
        try:
            safe = sanitize_name(name)
        except AttachmentError:
            log.warning("Пропускаю вложение с недопустимым именем: %r", name)
            continue
        path = base / safe
        if not path.is_file():
            log.warning("Вложение %s не найдено — пропускаю", safe)
            continue
        encoded.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    return encoded
