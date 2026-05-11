from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_bot_username: str = "Dimondra_Ai_Bot"
    webhook_url: str = ""
    webhook_secret: str = ""

    db_url: str = "postgresql+asyncpg://bot:changeme@db:5432/tgbot"
    db_password: str = "changeme"

    llm_provider: str = "stub"
    openai_api_key: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""

    # Telethon userbot
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    sync_history_days: int = 2

    # Auth & deploy
    session_secret: str = "change-me-session-secret"
    deploy_secret: str = ""
    owner_telegram_id: int = 169510539

    bot_mode: str = "manual"
    log_level: str = "INFO"
    api_secret_key: str = ""

    @property
    def db_url_asyncpg(self) -> str:
        url = self.db_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def db_url_sync(self) -> str:
        return self.db_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
