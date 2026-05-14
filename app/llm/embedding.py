import asyncio
import base64
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
_BASE_URL_V2 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent"
_DIMS = 768


class _EmbeddingQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue[tuple[asyncio.Future, Any]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _ensure_worker(self) -> None:
        async with self._lock:
            if self._worker and not self._worker.done():
                return
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            fut, coro = await self._q.get()
            try:
                res = await coro
                if not fut.cancelled():
                    fut.set_result(res)
            except Exception as exc:
                if not fut.cancelled():
                    fut.set_exception(exc)
            finally:
                self._q.task_done()

    async def submit(self, coro) -> Any:
        await self._ensure_worker()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await self._q.put((fut, coro))
        return await fut


def _is_gemini_prepay_depleted(resp: httpx.Response) -> bool:
    try:
        data = resp.json()
        msg = (((data.get("error") or {}).get("message")) or "").lower()
        return "prepayment credits are depleted" in msg or "billing" in msg and "ai.studio" in msg
    except Exception:
        return False


_queue: _EmbeddingQueue | None = None


def _get_queue() -> _EmbeddingQueue:
    global _queue
    if _queue is None:
        _queue = _EmbeddingQueue()
    return _queue


async def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    async def _job() -> list[float]:
        from app.services.tokens import get_token_manager
        mgr = get_token_manager()
        lease = await mgr.next_token("embed")
        if not lease:
            raise RuntimeError("No tokens with embed capability. Add one in Settings → API токены.")

        if lease.provider == "gemini":
            return await _embed_gemini(mgr, token_id=lease.id, token=lease.token, text=text, task_type=task_type)
        if lease.provider == "openai":
            return await _embed_openai(mgr, token_id=lease.id, token=lease.token, text=text)
        raise RuntimeError(f"Embedding provider not supported: {lease.provider}")

    return await _get_queue().submit(_job())


async def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    items = [str(t or "") for t in (texts or [])]
    if not items:
        return []
    if len(items) == 1:
        return [await embed_text(items[0], task_type=task_type)]

    async def _job() -> list[list[float]]:
        from app.services.tokens import get_token_manager
        mgr = get_token_manager()
        lease = await mgr.next_token("embed")
        if not lease:
            raise RuntimeError("No tokens with embed capability. Add one in Settings → API токены.")

        if lease.provider == "gemini":
            return await _embed_gemini_batch(mgr, token_id=lease.id, token=lease.token, texts=items, task_type=task_type)
        if lease.provider == "openai":
            return await _embed_openai_batch(mgr, token_id=lease.id, token=lease.token, texts=items)
        raise RuntimeError(f"Embedding provider not supported: {lease.provider}")

    return await _get_queue().submit(_job())


async def embed_gemini_multimodal(
    *,
    parts: list[dict],
    output_dimensionality: int = _DIMS,
) -> list[float]:
    async def _job() -> list[float]:
        from app.services.tokens import get_token_manager
        mgr = get_token_manager()
        lease = await mgr.next_token("embed", provider="gemini")
        if not lease:
            raise RuntimeError("No Gemini tokens with embed capability. Add one in Settings → API токены.")
        return await _embed_gemini_v2(mgr, token_id=lease.id, token=lease.token, parts=parts, output_dimensionality=output_dimensionality)

    return await _get_queue().submit(_job())


async def _embed_gemini(mgr: Any, *, token_id: int, token: str, text: str, task_type: str) -> list[float]:
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": _DIMS,
    }
    url = f"{_BASE_URL}?key={token}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            if _is_gemini_prepay_depleted(resp):
                await mgr.on_error(token_id)
                raise RuntimeError("Embedding API error 429: billing/prepayment credits depleted")
            await mgr.on_rate_limit(token_id)
            raise RuntimeError("Embedding API error 429: rate-limited")
        if resp.status_code >= 400:
            await mgr.on_error(token_id)
            raise RuntimeError(f"Embedding API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data["embedding"]["values"]
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        await mgr.on_error(token_id)
        raise RuntimeError(f"Embedding API network error: {str(exc)[:200]}") from exc


async def _embed_gemini_batch(mgr: Any, *, token_id: int, token: str, texts: list[str], task_type: str) -> list[list[float]]:
    payload = {
        "requests": [
            {
                "model": "models/gemini-embedding-001",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
                "outputDimensionality": _DIMS,
            }
            for t in texts
        ]
    }
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents?key=" + token
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            if _is_gemini_prepay_depleted(resp):
                await mgr.on_error(token_id)
                raise RuntimeError("Embedding API error 429: billing/prepayment credits depleted")
            await mgr.on_rate_limit(token_id)
            raise RuntimeError("Embedding API error 429: rate-limited")
        if resp.status_code >= 400:
            await mgr.on_error(token_id)
            raise RuntimeError(f"Embedding API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        embeddings = data.get("embeddings") or []
        out: list[list[float]] = []
        for e in embeddings:
            out.append(e["values"])
        if len(out) != len(texts):
            raise RuntimeError("Embedding API returned mismatched batch size")
        return out
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        await mgr.on_error(token_id)
        raise RuntimeError(f"Embedding API network error: {str(exc)[:200]}") from exc


async def _embed_gemini_v2(
    mgr: Any,
    *,
    token_id: int,
    token: str,
    parts: list[dict],
    output_dimensionality: int,
) -> list[float]:
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": parts},
        "outputDimensionality": int(output_dimensionality),
    }
    url = f"{_BASE_URL_V2}?key={token}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            if _is_gemini_prepay_depleted(resp):
                await mgr.on_error(token_id)
                raise RuntimeError("Embedding API error 429: billing/prepayment credits depleted")
            await mgr.on_rate_limit(token_id)
            raise RuntimeError("Embedding API error 429: rate-limited")
        if resp.status_code >= 400:
            await mgr.on_error(token_id)
            raise RuntimeError(f"Embedding API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        emb = data.get("embedding") or {}
        values = emb.get("values")
        if not isinstance(values, list):
            raise RuntimeError("Embedding API error: missing embedding values")
        return values
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        await mgr.on_error(token_id)
        raise RuntimeError(f"Embedding API network error: {str(exc)[:200]}") from exc


_openai_clients: dict[str, object] = {}


async def _embed_openai(mgr: Any, *, token_id: int, token: str, text: str) -> list[float]:
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


async def _embed_openai_batch(mgr: Any, *, token_id: int, token: str, texts: list[str]) -> list[list[float]]:
    from openai import AsyncOpenAI

    client = _openai_clients.get(token)
    if client is None:
        client = AsyncOpenAI(api_key=token)
        _openai_clients[token] = client

    try:
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=texts,
            dimensions=_DIMS,
        )
        return [d.embedding for d in resp.data]
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        text_exc = str(exc).lower()
        is_rate = status == 429 or "rate limit" in text_exc or "ratelimit" in text_exc
        if is_rate:
            await mgr.on_rate_limit(token_id)
        else:
            await mgr.on_error(token_id)
        raise


def inline_data_part(*, mime_type: str, data: bytes) -> dict:
    return {
        "inlineData": {
            "mimeType": str(mime_type),
            "data": base64.b64encode(data).decode(),
        }
    }
