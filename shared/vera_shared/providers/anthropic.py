import httpx

from vera_shared.providers.base import BaseProvider, ProviderError
from vera_shared.tokens.pool import get_pool
from vera_shared.tokens.selector import get_token

_BASE_URL = "https://api.anthropic.com/v1"
_MODEL = "claude-haiku-4-5"
_PROVIDER = "anthropic"


class AnthropicProvider(BaseProvider):
    name = _PROVIDER

    async def chat(
        self, messages: list[dict], capability: str = "chat:smart", system: str | None = None
    ) -> tuple[str, int, int]:
        provider_cap = capability if capability in ("chat:smart", "chat:code") else "chat:smart"
        token = await get_token(_PROVIDER, provider_cap)

        anth_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        payload: dict = {
            "model": _MODEL,
            "max_tokens": 2048,
            "messages": anth_messages,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": token.token,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{_BASE_URL}/messages", json=payload, headers=headers)

        if resp.status_code != 200:
            await get_pool().on_error(token.id, resp.status_code)
            raise ProviderError(
                f"Anthropic error {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        data = resp.json()
        text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        text = "".join(text_parts)
        usage = data.get("usage", {})
        return (text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("AnthropicProvider does not support embed")


_anthropic: AnthropicProvider | None = None


def get_anthropic() -> AnthropicProvider:
    global _anthropic
    if _anthropic is None:
        _anthropic = AnthropicProvider()
    return _anthropic
