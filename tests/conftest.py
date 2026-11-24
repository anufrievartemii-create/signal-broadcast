"""Тесты ходят в отдельный sqlite, а не в боевую data/app.db.

DATABASE_URL задаётся до импорта приложения: движок создаётся при загрузке app.db.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_db_path = Path(tempfile.gettempdir()) / "signal-broadcast-pytest.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path.as_posix()}"
