import logging

from openai import AsyncOpenAI
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ApiToken
from app.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

_DEEPSEEK_BASE = "https://api.deepseek.com"
_MODEL = "deepseek-chat"


class DeepSeekProvider(LLMProvider):
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    async def _client_or_raise(self) -> AsyncOpenAI:
        if self._client:
            return self._client
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(ApiToken)
                .where(ApiToken.provider == "deepseek", ApiToken.is_active.is_(True))
                .order_by(ApiToken.id)
                .limit(1)
            )).scalar_one_or_none()
        if not row:
            raise RuntimeError("No active DeepSeek token. Add one in Settings → API токены.")
        self._client = AsyncOpenAI(api_key=row.token, base_url=_DEEPSEEK_BASE)
        return self._client

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        client = await self._client_or_raise()
        payload = []
        if system_prompt:
            payload.append({"role": "system", "content": system_prompt})
        payload.extend({"role": m.role, "content": m.content} for m in messages)
        response = await client.chat.completions.create(
            model=_MODEL,
            messages=payload,
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""
