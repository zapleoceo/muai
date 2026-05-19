from abc import ABC, abstractmethod

from vera_shared.base_bot.task import Task, TaskResult
from vera_shared.providers.registry import get_registry
from vera_shared.tokens.model import TokenRecord
from vera_shared.tokens.selector import get_token


class BaseBot(ABC):
    agent_id: str
    name: str
    capabilities: list[str]
    required_caps: list[str]
    http_url: str
    bot_username: str | None = None

    @abstractmethod
    async def handle_task(self, task: Task) -> TaskResult:
        ...

    async def get_token(self, capability: str) -> TokenRecord:
        return await get_token(capability)

    async def chat(
        self, messages: list[dict], capability: str = "chat:fast"
    ) -> str:
        text, _, _ = await get_registry().chat(capability, messages)
        return text
