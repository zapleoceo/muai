import asyncio
import logging

import httpx
from openai import AsyncOpenAI

from app.llm.base import LLMMessage, LLMProvider
from app.llm.gemini_provider import GeminiContentError

logger = logging.getLogger(__name__)

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_MODEL = "gemini-2.5-flash"
_DEEPSEEK_BASE = "https://api.deepseek.com"
_DEEPSEEK_MODEL = "deepseek-chat"
_GROQ_BASE = "https://api.groq.com/openai/v1"
_GROQ_MODEL = "llama-3.3-70b-versatile"
_OPENAI_MODEL = "gpt-4o-mini"

_MAX_LEASE_ATTEMPTS = 12


class MultiProvider(LLMProvider):
    def __init__(self) -> None:
        self._openai_clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _client_for(self, *, provider: str, token: str) -> AsyncOpenAI:
        key = (provider, token)
        client = self._openai_clients.get(key)
        if client is not None:
            return client
        if provider == "deepseek":
            client = AsyncOpenAI(api_key=token, base_url=_DEEPSEEK_BASE)
        elif provider == "groq":
            client = AsyncOpenAI(api_key=token, base_url=_GROQ_BASE)
        else:
            client = AsyncOpenAI(api_key=token)
        self._openai_clients[key] = client
        return client

    async def complete(self, messages: list[LLMMessage], system_prompt: str = "") -> str:
        from app.services.tokens import get_token_manager

        mgr = get_token_manager()
        last_error: str | None = None

        for attempt in range(_MAX_LEASE_ATTEMPTS):
            lease = await mgr.next_token("chat")
            if not lease:
                raise RuntimeError("No tokens with chat capability. Add one in Settings → API токены.")

            try:
                if lease.provider == "gemini":
                    return await _complete_gemini(token=lease.token, messages=messages, system_prompt=system_prompt)
                if lease.provider == "deepseek":
                    return await _complete_openai_compat(
                        client=self._client_for(provider="deepseek", token=lease.token),
                        model=_DEEPSEEK_MODEL,
                        messages=messages,
                        system_prompt=system_prompt,
                    )
                if lease.provider == "groq":
                    return await _complete_openai_compat(
                        client=self._client_for(provider="groq", token=lease.token),
                        model=_GROQ_MODEL,
                        messages=messages,
                        system_prompt=system_prompt,
                    )
                if lease.provider == "openai":
                    return await _complete_openai_compat(
                        client=self._client_for(provider="openai", token=lease.token),
                        model=_OPENAI_MODEL,
                        messages=messages,
                        system_prompt=system_prompt,
                    )

                await mgr.on_error(lease.id)
                last_error = f"Unsupported chat provider: {lease.provider}"
                continue
            except GeminiContentError:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                text = str(exc)
                low = text.lower()

                is_rate = status == 429 or "rate limit" in low or "ratelimit" in low or " 429" in low
                is_insufficient = status == 402 or "insufficient balance" in low

                if is_rate:
                    await mgr.on_rate_limit(lease.id)
                    last_error = f"{lease.provider} 429"
                    continue

                if is_insufficient:
                    await mgr.on_error(lease.id)
                    last_error = f"{lease.provider} 402 insufficient balance"
                    continue

                await mgr.on_error(lease.id)
                last_error = f"{lease.provider} error: {text[:200]}"
                continue

        raise RuntimeError(f"All chat tokens failed. Last error: {last_error}")


async def _complete_openai_compat(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[LLMMessage],
    system_prompt: str,
) -> str:
    payload = []
    if system_prompt:
        payload.append({"role": "system", "content": system_prompt})
    payload.extend({"role": m.role, "content": m.content} for m in messages)
    resp = await client.chat.completions.create(model=model, messages=payload, max_tokens=1024)
    return resp.choices[0].message.content or ""


async def _complete_gemini(*, token: str, messages: list[LLMMessage], system_prompt: str) -> str:
    body = _build_gemini_body(messages, system_prompt)
    url = f"{_GEMINI_BASE_URL}/{_GEMINI_MODEL}:generateContent?key={token}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(url, content=body, headers={"Content-Type": "application/json"})
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"Gemini network error: {str(exc)[:200]}") from exc
    if resp.status_code == 429:
        raise RuntimeError("Gemini 429: rate limit")
    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return _parse_gemini_response(data)


def _build_gemini_body(messages: list[LLMMessage], system_prompt: str) -> bytes:
    import json

    contents = [
        {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
        for m in messages
    ]
    payload: dict = {"contents": contents}
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _parse_gemini_response(data: dict) -> str:
    feedback = data.get("promptFeedback", {})
    block_reason = feedback.get("blockReason")
    if block_reason:
        raise GeminiContentError(reason=f"prompt blocked by Gemini: {block_reason}", finish_reason="PROMPT_BLOCKED")

    candidates = data.get("candidates", [])
    if not candidates:
        raise GeminiContentError(reason="Gemini returned no candidates")

    candidate = candidates[0]
    finish = candidate.get("finishReason", "STOP")
    safety = candidate.get("safetyRatings", [])
    content = candidate.get("content", {})
    parts = content.get("parts", [])

    if finish not in ("STOP", "MAX_TOKENS") or not parts:
        blocked = [
            f"{r['category'].replace('HARM_CATEGORY_', '')}: {r['probability']}"
            for r in safety
            if r.get("probability") not in ("NEGLIGIBLE", "LOW")
        ]
        detail = ", ".join(blocked) if blocked else finish
        raise GeminiContentError(
            reason=f"response blocked: {detail}" if blocked else f"no content (finishReason={finish})",
            finish_reason=finish,
            safety_ratings=safety,
        )

    return parts[0].get("text", "")
