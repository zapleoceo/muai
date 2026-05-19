from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token_vera: str = "8583101764:AAEFp1W9gsLt8YzKspYW953lkASsm27Rx2E"
    vera_group_id: int = -1003939380118
    owner_telegram_id: int = 169510539
    deploy_secret: str = "changeme"
    session_secret: str = "changeme-session"
    db_path: str = "/data/vera.db"
    webhook_base_url: str = "https://dima.veranda.my"
    github_repo: str = "zapleoceo/muai"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
