import os
from dataclasses import dataclass


@dataclass
class Config:
    bot_token: str
    bot_name: str
    manager_url: str
    manager_inbox_secret: str
    executor_api_secret: str
    executor_api_port: int
    owner_chat_id: int | None


def get_config() -> Config:
    return Config(
        bot_token=os.environ["VERANDA_BOT_TOKEN"],
        bot_name=os.environ.get("EXECUTOR_BOT_NAME", "ВерандаБот"),
        manager_url=os.environ.get("MANAGER_URL", "http://bot:8000"),
        manager_inbox_secret=os.environ["EXECUTOR_INBOX_SECRET"],
        executor_api_secret=os.environ["EXECUTOR_API_SECRET"],
        executor_api_port=int(os.environ.get("EXECUTOR_API_PORT", "8001")),
        owner_chat_id=int(os.environ["OWNER_TELEGRAM_ID"]) if os.environ.get("OWNER_TELEGRAM_ID") else None,
    )
