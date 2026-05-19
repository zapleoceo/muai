import re

import google.generativeai as genai

import vera_shared.tokens.repository as token_repo
from vera_shared.providers.base import BaseProvider, ProviderError
from vera_shared.providers.pricing import cost_usd
from vera_shared.tokens.pool import TokensExhausted, get_pool
from vera_shared.tokens.selector import get_token

_MODEL = "gemini-flash-lite-latest"
_MAX_RETRIES = 3
_PROVIDER = "gemini"


class GeminiProvider(BaseProvider):
    name = _PROVIDER

    async def chat(
        self, messages: list[dict], capability: str = "chat:fast", system: str | None = None
    ) -> tuple[str, int, int]:
        last_exc: Exception | None = None

        for _ in range(_MAX_RETRIES):
            token = await get_token(_PROVIDER, capability)
            genai.configure(api_key=token.token)
            model = genai.GenerativeModel(_MODEL, system_instruction=system)

            contents = _to_gemini_contents(messages)
            try:
                response = await model.generate_content_async(contents)
                usage = response.usage_metadata
                t_in = usage.prompt_token_count or 0
                t_out = usage.candidates_token_count or 0
                await token_repo.record_usage(
                    token.id, t_in, t_out, cost_usd(_PROVIDER, _MODEL, t_in, t_out)
                )
                return (response.text, t_in, t_out)
            except Exception as exc:
                status = _parse_status(exc)
                retry_after = _parse_retry_after(exc) if status == 429 else None
                await get_pool().on_error(token.id, status, retry_after_seconds=retry_after)
                last_exc = exc
                if status != 429:
                    break

        raise ProviderError(str(last_exc), status_code=_parse_status(last_exc)) from last_exc

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("GeminiProvider does not support embed")


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    role_map = {"user": "user", "assistant": "model", "system": "user"}
    return [{"role": role_map.get(m["role"], "user"), "parts": [m["content"]]} for m in messages]


def _parse_status(exc: Exception | None) -> int:
    if exc is None:
        return 0
    text = str(exc).lower()
    if "429" in text or "quota" in text or "rate" in text or "resource_exhausted" in text:
        return 429
    if "401" in text or "403" in text or "api_key" in text or "permission_denied" in text:
        return 401
    return 500


_RETRY_RE = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.IGNORECASE)


def _parse_retry_after(exc: Exception) -> int | None:
    m = _RETRY_RE.search(str(exc))
    return int(m.group(1)) if m else None


_gemini: GeminiProvider | None = None


def get_gemini() -> GeminiProvider:
    global _gemini
    if _gemini is None:
        _gemini = GeminiProvider()
    return _gemini
