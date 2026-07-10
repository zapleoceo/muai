"""Thin Vera ↔ AIbroker client.

When BROKER_URL is set, Vera's chat() and embed() route here instead of
talking to providers directly. Broker handles key selection, cost guard,
fallback chain, and per-key cooldowns. We just pass capability + content.

Same signature/return as the legacy client so callers don't change.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from vera_shared.db.engine import get_session
from vera_shared.db.models import UsageLogRow

log = logging.getLogger(__name__)

BROKER_URL = os.environ.get("BROKER_URL", "").rstrip("/")
BROKER_PROJECT_KEY = os.environ.get("BROKER_PROJECT_KEY", "")
BROKER_TIMEOUT_S = float(os.environ.get("BROKER_TIMEOUT_S", "120"))
# submit+poll (/v1/jobs) client-side ceiling — matches the old sync
# BROKER_TIMEOUT_S (120s/2min), same expectation callers had before the
# migration. Broker itself marks a job 'error' (timeout) after ~20 min
# server-side; this is a tighter client-side bound on top of that.
JOB_POLL_DEADLINE_S = float(os.environ.get("BROKER_JOB_DEADLINE_S", "120"))


def broker_enabled() -> bool:
    return bool(BROKER_URL and BROKER_PROJECT_KEY)


class BrokerCallFailed(Exception):
    """Broker returned non-2xx or all providers exhausted."""


# Shared httpx client — TLS handshake reuse for performance
_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        async with _http_lock:
            # double-check: пока ждали лок, другая корутина могла уже создать
            if _http is None:
                _http = httpx.AsyncClient(
                    timeout=BROKER_TIMEOUT_S,
                    headers={"X-Project-Key": BROKER_PROJECT_KEY},
                )
    return _http


async def _log_usage(meta: dict[str, Any], workflow: str | None,
                      event_id: int | None, capability: str | None) -> None:
    """Mirror broker's response into Vera's usage_log so dashboard works."""
    try:
        async with get_session() as s:
            s.add(UsageLogRow(
                # .get(key, default) only falls back when the KEY is absent —
                # broker responses seen under concurrent load can have the
                # key present with an explicit null (job raced/errored
                # mid-write), which .get() would pass straight through as
                # None into a NOT NULL column. `or` catches both cases.
                provider=meta.get("provider") or "broker",
                model=meta.get("model") or "",
                capability=capability or "",
                tokens_in=int(meta.get("tokens_in") or 0),
                tokens_out=int(meta.get("tokens_out") or 0),
                cost_usd=float(meta.get("cost_usd") or 0),
                latency_ms=int(meta.get("latency_ms") or 0),
                success=True,
                workflow=workflow or "",
                event_id=event_id,
                request_id=(str(rid) if (rid := meta.get("request_id")) is not None else None),
                key_label=meta.get("key_label"),
            ))
    except Exception as e:
        log.warning("usage_log write failed: %s", e)


async def chat_via_broker(
    *,
    messages: list[dict[str, Any]],
    capability: str = "chat:fast",
    response_format: dict[str, Any] | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.5,
    workflow: str | None = None,
    event_id: int | None = None,
    model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Drop-in replacement for the legacy chat()."""
    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "workflow": workflow,
    }
    if model:
        payload["model"] = model
    if response_format:
        payload["response_format"] = response_format

    c = await _client()
    try:
        r = await c.post(
            f"{BROKER_URL}/v1/chat", params={"capability": capability}, json=payload,
        )
    except Exception as e:
        raise BrokerCallFailed(f"broker network: {e}") from e

    if r.status_code >= 400:
        raise BrokerCallFailed(f"broker {r.status_code}: {r.text[:200]}")

    data = r.json()
    text = data.get("text", "")
    meta = {
        "provider": data.get("provider"),
        "model": data.get("model"),
        "tokens_in": data.get("tokens_in", 0),
        "tokens_out": data.get("tokens_out", 0),
        "cost_usd": data.get("cost_usd", 0.0),
        "latency_ms": data.get("latency_ms", 0),
        "request_id": data.get("request_id"),
        "key_label": data.get("key_label"),
    }
    await _log_usage(meta, workflow, event_id, capability)
    return text, meta


async def chat_async_via_broker(
    *,
    messages: list[dict[str, Any]],
    capability: str = "chat:fast",
    response_format: dict[str, Any] | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.5,
    workflow: str | None = None,
    event_id: int | None = None,
    model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit+poll against /v1/jobs — same request/response contract as
    chat_via_broker, but never holds the connection open. A slow provider can
    only delay the poll loop, not 504 the caller. See docs/llm-broker.md.
    """
    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "workflow": workflow,
    }
    if model:
        payload["model"] = model
    if response_format:
        payload["response_format"] = response_format

    c = await _client()
    try:
        r = await c.post(
            f"{BROKER_URL}/v1/jobs", params={"capability": capability}, json=payload,
        )
    except Exception as e:
        raise BrokerCallFailed(f"broker network: {e}") from e
    if r.status_code >= 400:
        raise BrokerCallFailed(f"broker {r.status_code}: {r.text[:200]}")

    job = r.json()
    job_id = job["job_id"]
    poll_after_s = float(job.get("poll_after_s") or 2)
    deadline = time.monotonic() + JOB_POLL_DEADLINE_S

    while True:
        await asyncio.sleep(poll_after_s)
        try:
            r = await c.get(f"{BROKER_URL}/v1/jobs/{job_id}")
        except Exception as e:
            raise BrokerCallFailed(f"broker poll network: {e}") from e
        if r.status_code >= 400:
            raise BrokerCallFailed(f"broker poll {r.status_code}: {r.text[:200]}")

        data = r.json()
        status = data.get("status")
        if status == "pending":
            if time.monotonic() > deadline:
                raise BrokerCallFailed(
                    f"job {job_id} still pending after {JOB_POLL_DEADLINE_S:.0f}s"
                )
            poll_after_s = float(data.get("poll_after_s") or poll_after_s)
            continue
        if status == "error":
            raise BrokerCallFailed(f"job {job_id} failed: {data.get('error')}")
        if status != "done":
            # Malformed/unexpected response (bad JSON, new status we don't
            # know about) must NOT be treated as a silent success with an
            # empty answer — that's worse than a clear error.
            raise BrokerCallFailed(f"job {job_id} unexpected status: {status!r}")

        text = data.get("text", "")
        meta = {
            "provider": data.get("provider"),
            "model": data.get("model"),
            "tokens_in": data.get("tokens_in", 0),
            "tokens_out": data.get("tokens_out", 0),
            "cost_usd": data.get("cost_usd", 0.0),
            "latency_ms": data.get("latency_ms", 0),
            "request_id": data.get("request_id"),
            "key_label": data.get("key_label"),
        }
        await _log_usage(meta, workflow, event_id, capability)
        return text, meta


async def embed_via_broker(texts: str | list[str]) -> list[list[float]]:
    """Drop-in replacement for the legacy embed()."""
    inputs = [texts] if isinstance(texts, str) else list(texts)
    c = await _client()
    try:
        r = await c.post(
            f"{BROKER_URL}/v1/embed",
            params={"provider": "voyage"},
            json={"input": inputs},
        )
    except Exception as e:
        raise BrokerCallFailed(f"broker network: {e}") from e
    if r.status_code >= 400:
        raise BrokerCallFailed(f"broker {r.status_code}: {r.text[:200]}")
    data = r.json()
    meta = {
        "provider": "voyage",
        "model": data.get("model"),
        "tokens_in": data.get("tokens_in", 0),
        "tokens_out": 0,
        "cost_usd": data.get("cost_usd", 0.0),
        "latency_ms": data.get("latency_ms", 0),
        "request_id": data.get("request_id"),
        "key_label": data.get("key_label"),
    }
    await _log_usage(meta, "embed", None, "embedding")
    return data.get("embeddings", [])
