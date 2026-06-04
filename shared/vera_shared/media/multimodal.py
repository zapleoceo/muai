"""Gemini multimodal helper — bytes (audio/image/pdf/...) → text.

Uses our Gemini token pool (rotation). Direct HTTPS to
generativelanguage.googleapis.com so we don't depend on a specific SDK
version across services.
"""

import base64
import logging

import httpx

from vera_shared.providers.pricing import cost_usd
from vera_shared.tokens import repository as token_repo
from vera_shared.tokens.pool import get_pool
from vera_shared.tokens.selector import get_token

log = logging.getLogger(__name__)

from vera_shared.llm.registry import PROVIDER_MODEL

_PROVIDER = "gemini"
_MAX_BYTES = 20 * 1024 * 1024


def _model() -> str:
    """Resolved at call time so registry changes don't require restart."""
    return PROVIDER_MODEL.get(_PROVIDER, "gemini-2.5-flash")


async def media_to_text(mime_type: str, data: bytes, instruction: str) -> str:
    if len(data) > _MAX_BYTES:
        return f"⚠ Файл слишком большой ({len(data) // 1024 // 1024} MB > 20 MB)"

    token = await get_token(_PROVIDER, "chat:fast")
    model = _model()
    # API key MUST go in the x-goog-api-key header, not the URL — otherwise
    # it gets captured by every proxy / nginx access log / httpx debug trace.
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(data).decode(),
                }},
                {"text": instruction},
            ],
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
    }

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(url, json=payload,
                         headers={"x-goog-api-key": token.token})

    if r.status_code != 200:
        await get_pool().on_error(token.id, r.status_code)
        log.warning("Gemini multimodal %d: %s", r.status_code, r.text[:200])
        return f"⚠ Gemini error {r.status_code}"

    data_resp = r.json()
    candidate = (data_resp.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    usage = data_resp.get("usageMetadata", {})
    t_in = usage.get("promptTokenCount", 0)
    t_out = usage.get("candidatesTokenCount", 0)
    await token_repo.record_usage(
        token.id, t_in, t_out, cost_usd(_PROVIDER, model, t_in, t_out)
    )
    return text or "(пустой ответ от Gemini)"
