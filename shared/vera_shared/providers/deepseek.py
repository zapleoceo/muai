import httpx

import vera_shared.tokens.repository as token_repo
from vera_shared.providers.base import BaseProvider, ProviderError
from vera_shared.providers.pricing import cost_usd
from vera_shared.tokens.pool import get_pool
from vera_shared.tokens.selector import get_token

_BASE_URL = "https://api.deepseek.com/v1"
_MODEL = "deepseek-chat"
_CAPABILITY = "chat:smart"
_PROVIDER = "deepseek"


class DeepSeekProvider(BaseProvider):
    name = _PROVIDER

    async def chat(
        self, messages: list[dict], capability: str = _CAPABILITY, system: str | None = None
    ) -> tuple[str, int, int]:
        # DeepSeek only has chat:smart / chat:code natively; map fast/prefilter onto smart.
        provider_cap = capability if capability in ("chat:smart", "chat:code") else "chat:smart"
        token = await get_token(_PROVIDER, provider_cap)
        msgs = ([{"role": "system", "content": system}] + messages) if system else messages
        payload = {
            "model": _MODEL,
            "messages": msgs,
        }
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{_BASE_URL}/chat/completions", json=payload, headers=headers)

        if resp.status_code != 200:
            await get_pool().on_error(token.id, resp.status_code)
            raise ProviderError(
                f"DeepSeek error {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )

        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        t_in = usage.get("prompt_tokens", 0)
        t_out = usage.get("completion_tokens", 0)
        await token_repo.record_usage(
            token.id, t_in, t_out, cost_usd(_PROVIDER, _MODEL, t_in, t_out)
        )
        return (choice, t_in, t_out)

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("DeepSeekProvider does not support embed")


_deepseek: DeepSeekProvider | None = None


def get_deepseek() -> DeepSeekProvider:
    global _deepseek
    if _deepseek is None:
        _deepseek = DeepSeekProvider()
    return _deepseek
