import asyncio
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_DIMS = 768


async def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    from app.services.tokens import get_token_manager
    mgr = get_token_manager()

    lease = await mgr.next_token("embed")
    if not lease:
        raise RuntimeError("No tokens with embed capability. Add one in Settings → API токены.")

    if lease.provider == "gemini":
        return await _embed_gemini(mgr, lease.id, lease.token, text, task_type)
    if lease.provider == "openai":
        return await _embed_openai(mgr, lease.id, lease.token, text)
    raise RuntimeError(f"Embedding provider not supported: {lease.provider}")


async def _embed_gemini(mgr, token_id: int, token: str, text: str, task_type: str) -> list[float]:
    base_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
    payload = json.dumps({
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": _DIMS,
    }).encode()

    req = urllib.request.Request(
        f"{base_url}?key={token}",
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
        if exc.code == 429:
            await mgr.on_rate_limit(token_id)
        else:
            await mgr.on_error(token_id)
        raise RuntimeError(f"Embedding API error {exc.code}: {body[:200]}") from exc


_openai_clients: dict[str, object] = {}


async def _embed_openai(mgr, token_id: int, token: str, text: str) -> list[float]:
    from openai import AsyncOpenAI

    client = _openai_clients.get(token)
    if client is None:
        client = AsyncOpenAI(api_key=token)
        _openai_clients[token] = client

    try:
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
            dimensions=_DIMS,
        )
        return resp.data[0].embedding
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        text_exc = str(exc).lower()
        is_rate = status == 429 or "rate limit" in text_exc or "ratelimit" in text_exc
        if is_rate:
            await mgr.on_rate_limit(token_id)
        else:
            await mgr.on_error(token_id)
        raise
