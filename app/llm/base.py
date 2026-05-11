from abc import ABC, abstractmethod


class LLMMessage:
    def __init__(self, role: str, content: str):
        self.role = role        # "user" | "assistant" | "system"
        self.content = content


class LLMProvider(ABC):
    @abstractmethod
    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        """Send messages to LLM and return the reply text."""
