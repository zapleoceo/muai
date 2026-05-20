from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token_vera: str = ""
    vera_group_id: int = 0
    owner_telegram_id: int = 0
    deploy_secret: str = ""
    session_secret: str = ""
    internal_secret: str = ""
    db_path: str = "/data/vera.db"
    webhook_base_url: str = "https://dima.veranda.my"
    github_repo: str = "zapleoceo/muai"

    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    gmail_client_id: str = ""
    gmail_client_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def _ensure_secrets(s: Settings) -> None:
    missing = [
        name for name in
        ("telegram_bot_token_vera", "owner_telegram_id", "vera_group_id",
         "deploy_secret", "session_secret", "internal_secret")
        if not getattr(s, name)
    ]
    if missing:
        raise RuntimeError(
            "Refusing to start: required .env keys missing: " + ", ".join(missing)
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    _ensure_secrets(s)
    return s
