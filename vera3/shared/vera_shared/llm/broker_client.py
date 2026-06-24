"""Thin Vera ↔ AIbroker client.

When BROKER_URL is set, Vera's chat() and embed() route here instead of
talking to providers directly. Broker handles key selection, cost guard,
fallback chain, and per-key cooldowns. We just pass capability + content.

Same signature/return as the legacy client so callers don't change.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from vera_shared.db.engine import get_session
from vera_shared.db.models import UsageLogRow

log = logging.getLogger(__name__)

BROKER_URL = os.environ.get("BROKER_URL", "").rstrip("/")
BROKER_PROJECT_KEY = os.environ.get("BROKER_PROJECT_KEY", "")
BROKER_TIMEOUT_S = float(os.environ.get("BROKER_TIMEOUT_S", "120"))


def broker_enabled() -> bool:
    return bool(BROKER_URL and BROKER_PROJECT_KEY)


class BrokerCallFailed(Exception):
    """Broker returned non-2xx or all providers exhausted."""


# Shared httpx client — TLS handshake reuse for performance
_http: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http
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
                provider=meta.get("provider", "broker"),
                model=meta.get("model", ""),
                capability=capability or "",
                tokens_in=int(meta.get("tokens_in") or 0),
                tokens_out=int(meta.get("tokens_out") or 0),
                cost_usd=float(meta.get("cost_usd") or 0),
                latency_ms=int(meta.get("latency_ms") or 0),
                success=True,
                workflow=workflow or "",
                event_id=event_id,
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
    }
    await _log_usage(meta, "embed", None, "embedding")
    return data.get("embeddings", [])
