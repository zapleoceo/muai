import asyncio
import base64
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
_BASE_URL_V2 = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent"
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_VOYAGE_MODEL = "voyage-3-lite"  # native 512 dims; 200M tokens/month free
_DIMS = 512  # voyage-3-lite=512 native; gemini-embedding-001 supports reduction to 512


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


async def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT", capability: str = "embed") -> list[float]:
    async def _job() -> list[float]:
        from app.services.tokens import get_token_manager
        mgr = get_token_manager()
        lease = await mgr.next_token(capability)
        # if a non-default capability found no token, fall back to generic embed
        if lease is None and capability != "embed":
            lease = await mgr.next_token("embed")
        if not lease:
            raise RuntimeError("No tokens with embed capability. Add one in Settings → API токены.")

        if lease.provider == "gemini":
            return await _embed_gemini(mgr, token_id=lease.id, token=lease.token, text=text, task_type=task_type)
        if lease.provider == "openai":
            return await _embed_openai(mgr, token_id=lease.id, token=lease.token, text=text)
        if lease.provider == "voyage":
            result = await _embed_voyage_batch(mgr, token_id=lease.id, token=lease.token, texts=[text])
            return result[0]
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
        if lease.provider == "voyage":
            return await _embed_voyage_batch(mgr, token_id=lease.id, token=lease.token, texts=items)
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
        # Request embed_media specifically — lets users designate dedicated Gemini keys
        # for file/multimodal embedding, separate from text-embed quota.
        lease = await mgr.next_token("embed_media", provider="gemini")
        if not lease:
            # Fall back to any Gemini embed key if no embed_media-specific key exists.
            lease = await mgr.next_token("embed", provider="gemini")
        if not lease:
            raise RuntimeError("No Gemini tokens with embed_media capability. Add one in Settings → API токены.")
        return await _embed_gemini_v2(mgr, token_id=lease.id, token=lease.token, parts=parts, output_dimensionality=output_dimensionality)

    return await _get_queue().submit(_job())


async def _embed_gemini(mgr: Any, *, token_id: int, token: str, text: str, task_type: str) -> list[float]:
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": _DIMS,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(_BASE_URL, json=payload, headers={"x-goog-api-key": token})
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
    _BATCH_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(_BATCH_URL, json=payload, headers={"x-goog-api-key": token})
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
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            resp = await client.post(_BASE_URL_V2, json=payload, headers={"x-goog-api-key": token})
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


async def _embed_voyage_batch(mgr: Any, *, token_id: int, token: str, texts: list[str]) -> list[list[float]]:
    # voyage-3 supports output_dimension up to 1024; we use 512 to match schema.
    # Batch limit: 128 inputs per request.
    payload = {
        "model": _VOYAGE_MODEL,
        "input": texts,
        "input_type": "document",
        # voyage-3-lite native output is 512; no output_dimension needed
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(
                _VOYAGE_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        if resp.status_code == 429:
            await mgr.on_rate_limit(token_id)
            raise RuntimeError("Voyage embedding API error 429: rate-limited")
        if resp.status_code >= 400:
            await mgr.on_error(token_id)
            raise RuntimeError(f"Voyage embedding API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        items = sorted(data.get("data") or [], key=lambda x: x["index"])
        if len(items) != len(texts):
            raise RuntimeError("Voyage embedding API: mismatched batch size")
        return [item["embedding"] for item in items]
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        await mgr.on_error(token_id)
        raise RuntimeError(f"Voyage embedding network error: {str(exc)[:200]}") from exc


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


async def _transcribe_once(mgr: Any, lease: Any, payload: dict) -> str:
    """Single transcription attempt. Raises RuntimeError on any failure."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={lease.token}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(url, json=payload)
    except (httpx.TransportError, asyncio.TimeoutError) as exc:
        await mgr.on_error(lease.id)
        raise RuntimeError(f"Transcription network error: {str(exc)[:200]}") from exc
    if resp.status_code == 429:
        await mgr.on_rate_limit(lease.id)
        raise RuntimeError(f"token {lease.id} rate-limited")
    if resp.status_code >= 400:
        await mgr.on_error(lease.id)
        raise RuntimeError(f"Transcription API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    candidates = data.get("candidates") or []
    if candidates:
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if parts:
            return str(parts[0].get("text") or "").strip()
    raise RuntimeError("Transcription: empty response")


async def transcribe_audio_gemini(*, mime_type: str, data: bytes) -> str:
    """Transcribe audio using Gemini. Exhausts all immediately available tokens, then waits once."""
    from app.services.tokens import get_token_manager
    mgr = get_token_manager()

    b64 = base64.b64encode(data).decode()
    payload = {
        "contents": [{
            "parts": [
                {"text": "Transcribe this voice message accurately. Return only the transcription text, nothing else."},
                {"inlineData": {"mimeType": str(mime_type), "data": b64}},
            ]
        }],
        "generationConfig": {"maxOutputTokens": 1024},
    }

    tried: set[int] = set()
    last_error = "No Gemini tokens available for transcription"

    # Phase 1: try every immediately available Gemini token without blocking
    for _ in range(20):
        lease = await mgr.next_token("chat", provider="gemini", max_wait=0)
        if not lease or lease.id in tried:
            break
        tried.add(lease.id)
        try:
            return await _transcribe_once(mgr, lease, payload)
        except RuntimeError as exc:
            last_error = str(exc)
            logger.warning("Transcription attempt failed: %s", exc)

    # Phase 2: all immediately available tokens exhausted — wait for one to free up
    lease = await mgr.next_token("chat", provider="gemini", max_wait=120.0)
    if lease:
        try:
            return await _transcribe_once(mgr, lease, payload)
        except RuntimeError as exc:
            last_error = str(exc)
            logger.warning("Transcription fallback attempt failed: %s", exc)

    raise RuntimeError(last_error)


async def transcribe_audio_gemini_queued(*, mime_type: str, data: bytes) -> str:
    """Queued version for batch/background use — serialized through the embedding queue."""
    return await _get_queue().submit(transcribe_audio_gemini(mime_type=mime_type, data=data))


def inline_data_part(*, mime_type: str, data: bytes) -> dict:
    return {
        "inlineData": {
            "mimeType": str(mime_type),
            "data": base64.b64encode(data).decode(),
        }
    }
