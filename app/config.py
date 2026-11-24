"""Конфигурация приложения: читается из переменных окружения и файла .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Приложение ---
    app_name: str = "signal-broadcast"
    host: str = "127.0.0.1"
    port: int = 8000
    admin_token: str = ""
    log_level: str = "INFO"

    # --- Signal ---
    signal_backend: str = "rest_api"  # rest_api | jsonrpc | fake
    signal_api_url: str = "http://127.0.0.1:8080"
    signal_api_timeout: float = 60.0
    signal_device_name: str = "signal-broadcast"

    # --- База ---
    database_url: str = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'app.db').as_posix()}"

    # --- Темп рассылки (throughput): короткие паузы, высокие лимиты ---
    delay_min: float = 0.4
    delay_max: float = 1.2
    max_retries: int = 2
    hourly_cap: int = 600
    daily_cap: int = 4000

    # --- Входящие / отписки ---
    stop_words: str = "стоп,stop,отписка,unsubscribe"
    auto_opt_out: bool = True

    # --- Пути ---
    data_dir: Path = PROJECT_ROOT / "data"
    uploads_dir: Path = PROJECT_ROOT / "data" / "uploads"

    @property
    def stop_words_list(self) -> list[str]:
        """Список стоп-слов (регистронезависимо, пустые отброшены)."""
        return [w.strip().lower() for w in self.stop_words.split(",") if w.strip()]

    def ensure_dirs(self) -> None:
        """Создаёт каталоги для БД и вложений."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
