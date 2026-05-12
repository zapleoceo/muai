import logging

from openai import AsyncOpenAI
from app.llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

_DEEPSEEK_BASE = "https://api.deepseek.com"
_MODEL = "deepseek-chat"
_MAX_RETRIES = 3


class DeepSeekProvider(LLMProvider):
    def __init__(self) -> None:
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client_for(self, token: str) -> AsyncOpenAI:
        if token not in self._clients:
            self._clients[token] = AsyncOpenAI(api_key=token, base_url=_DEEPSEEK_BASE)
        return self._clients[token]

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        payload = []
        if system_prompt:
            payload.append({"role": "system", "content": system_prompt})
        payload.extend({"role": m.role, "content": m.content} for m in messages)

        from app.services.tokens import get_token_manager
        mgr = get_token_manager()

        for attempt in range(_MAX_RETRIES):
            token = await mgr.next_token("deepseek")
            if not token:
                raise RuntimeError("No active DeepSeek token. Add one in Settings → API токены.")

            client = self._client_for(token)
            try:
                response = await client.chat.completions.create(
                    model=_MODEL,
                    messages=payload,
                    max_tokens=1024,
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                text = str(exc)
                is_rate_limit = status == 429 or "rate limit" in text.lower() or "ratelimit" in text.lower()
                if is_rate_limit:
                    await mgr.on_rate_limit("deepseek", token)
                    logger.warning("DeepSeek 429 on attempt %d, rotating token", attempt + 1)
                    if attempt == _MAX_RETRIES - 1:
                        raise RuntimeError("All DeepSeek tokens rate-limited") from exc
                    continue
                await mgr.on_error("deepseek", token)
                raise
