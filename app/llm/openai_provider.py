import logging

from openai import AsyncOpenAI

from app.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: str | None = None):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        payload = []
        if system_prompt:
            payload.append({"role": "system", "content": system_prompt})
        payload.extend({"role": m.role, "content": m.content} for m in messages)

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=payload,
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""
