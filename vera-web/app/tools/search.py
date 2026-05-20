import logging

import httpx

from vera_shared.providers.pricing import cost_usd
from vera_shared.tokens import repository as token_repo
from vera_shared.tokens.pool import get_pool
from vera_shared.tokens.selector import get_token

log = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
_PROVIDER = "gemini"


async def web_search(query: str) -> dict:
    token = await get_token(_PROVIDER, "chat:fast")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{_MODEL}:generateContent?key={token.token}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=payload)

    if r.status_code != 200:
        await get_pool().on_error(token.id, r.status_code)
        return {"error": f"gemini {r.status_code}: {r.text[:200]}"}

    data = r.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    answer = "".join(p.get("text", "") for p in parts).strip()

    sources: list[dict] = []
    gm = candidate.get("groundingMetadata") or {}
    for chunk in gm.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({
                "title": web.get("title", ""),
                "url": web["uri"],
            })

    queries_used = gm.get("webSearchQueries") or []

    usage = data.get("usageMetadata", {})
    t_in = usage.get("promptTokenCount", 0)
    t_out = usage.get("candidatesTokenCount", 0)
    await token_repo.record_usage(
        token.id, t_in, t_out, cost_usd(_PROVIDER, _MODEL, t_in, t_out)
    )

    return {
        "answer": answer,
        "sources": sources,
        "queries_used": queries_used,
    }
