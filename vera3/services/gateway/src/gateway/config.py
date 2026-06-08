"""Gateway конфигурация — из env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфиг загружается из env. См. infra/.env.example."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Auth
    internal_secret: str = ""           # для service-to-service
    owner_telegram_id: int = 0          # для admin auth

    # Webhook secrets
    telegram_webhook_secret: str = ""
    manychat_webhook_secret: str = ""

    # External services
    hatchet_url: str = "http://hatchet:7077"

    # Database
    database_url: str = "postgresql+asyncpg://vera:vera@postgres:5432/vera"

    # Logging
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
