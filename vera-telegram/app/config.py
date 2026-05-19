from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    session_path: str = "/data/sessions/userbot.session"
    vera_core_url: str = "http://vera-core:8000"
    db_path: str = "/data/vera.db"

    model_config = SettingsConfigDict(env_file=".env")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
