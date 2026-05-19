from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    vera_core_url: str = "http://vera-core:8000"
    repo_path: str = "/workspace"
    deploy_secret: str = ""
    deploy_url: str = "http://vera-core:8000/deploy"
    internal_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
