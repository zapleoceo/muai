from abc import ABC, abstractmethod


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int = 0) -> None:
        self.status_code = status_code
        super().__init__(message)


class BaseProvider(ABC):
    name: str

    @abstractmethod
    async def chat(
        self, messages: list[dict], capability: str = "chat:fast", system: str | None = None
    ) -> tuple[str, int, int]:
        # Returns (text, input_tokens, output_tokens)
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        ...
