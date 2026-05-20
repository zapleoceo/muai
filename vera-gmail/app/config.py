import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    vera_core_url: str = "http://vera-core:8000"
    internal_secret: str = ""
    session_secret: str = ""
    db_path: str = "/data/vera.db"
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    poll_interval_sec: int = 90
    poll_lookback_minutes: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    os.environ.setdefault("DB_PATH", s.db_path)
    return s
