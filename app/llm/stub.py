from app.llm.base import LLMMessage, LLMProvider


class StubProvider(LLMProvider):
    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        return (
            "LLM не настроен. Укажите LLM_PROVIDER и соответствующий API-ключ в .env.\n"
            f"Получено {len(messages)} сообщений в контексте."
        )
