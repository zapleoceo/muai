from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    internal_secret: str = ""
    vera_core_url: str = "http://vera-core:8000"
    repo_url: str = "https://github.com/zapleoceo/muai.git"
    repo_branch_base: str = "master"
    work_dir: str = "/work"
    db_path: str = "/data/vera.db"

    # Safety knobs
    max_iterations: int = 25
    max_changes_per_task: int = 8        # max distinct files edited
    rate_limit_per_hour: int = 1
    forbidden_paths: list[str] = [
        ".env", ".github/workflows/", "scripts/deploy.sh",
        "*.session", "secrets/",
    ]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
