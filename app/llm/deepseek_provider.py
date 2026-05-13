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
            lease = await mgr.next_token("chat", provider="deepseek")
            if not lease:
                raise RuntimeError("No active DeepSeek token. Add one in Settings → API токены.")

            client = self._client_for(lease.token)
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
                is_insufficient = status == 402 or "insufficient balance" in text.lower()
                is_auth = status in (401, 403) or "invalid api key" in text.lower() or "authentication" in text.lower()
                if is_rate_limit:
                    await mgr.on_rate_limit(lease.id)
                    logger.warning("DeepSeek 429 on attempt %d, rotating token", attempt + 1)
                    if attempt == _MAX_RETRIES - 1:
                        raise RuntimeError("All DeepSeek tokens rate-limited") from exc
                    continue
                if is_insufficient:
                    await mgr.on_error(lease.id)
                    raise RuntimeError("DeepSeek 402: insufficient balance. Пополни баланс или замени токен.") from exc
                if is_auth:
                    await mgr.on_error(lease.id)
                    raise RuntimeError("DeepSeek auth error: invalid token. Проверь API ключ или добавь новый.") from exc
                await mgr.on_error(lease.id)
                raise RuntimeError(f"DeepSeek error: {text[:200]}") from exc
