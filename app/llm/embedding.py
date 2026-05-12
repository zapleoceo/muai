import asyncio
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
_DIMS = 768


async def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Return a 768-dim embedding vector via Gemini text-embedding-004."""
    from app.services.tokens import get_token_manager
    token = await get_token_manager().next_token("gemini")
    if not token:
        raise RuntimeError("No Gemini tokens available for embedding")

    payload = json.dumps({
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": 768,
    }).encode()

    req = urllib.request.Request(
        f"{_BASE_URL}?key={token}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _call() -> list[float]:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["embedding"]["values"]

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _call)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace") if hasattr(exc, "read") else ""
        mgr = get_token_manager()
        if exc.code == 429:
            await mgr.on_rate_limit("gemini", token)
        else:
            await mgr.on_error("gemini", token)
        raise RuntimeError(f"Embedding API error {exc.code}: {body[:200]}") from exc
