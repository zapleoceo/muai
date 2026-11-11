"""Thin Vera ↔ AIbroker client.

When BROKER_URL is set, Vera's chat() and embed() route here instead of
talking to providers directly. Broker handles key selection, cost guard,
fallback chain, and per-key cooldowns. We just pass capability + content.

Same signature/return as the legacy client so callers don't change.

Отказы — `broker_errors`, цикл опроса джобы — `broker_jobs`; имена
реэкспортируются отсюда. Конфиг, общий HTTP-клиент и зеркало в usage_log
остаются здесь: тесты подменяют их как атрибуты этого модуля.
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
from vera_shared.llm.broker_errors import (
    BrokerCallFailed,
    BrokerJobErrored,
    BrokerJobGone,
    BrokerJobPending,
    _short,
)
from vera_shared.llm.broker_jobs import poll_job

__all__ = [
    "BrokerCallFailed",
    "BrokerJobErrored",
    "BrokerJobGone",
    "BrokerJobPending",
    "_short",
    "broker_enabled",
    "chat_async_via_broker",
    "chat_via_broker",
    "embed_via_broker",
    "time",
]

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


async def _post(c: httpx.AsyncClient, path: str, params: dict[str, str],
                payload: dict[str, Any]) -> dict[str, Any]:
    try:
        r = await c.post(f"{BROKER_URL}{path}", params=params, json=payload)
    except Exception as e:
        raise BrokerCallFailed(f"broker network: {e}") from e
    if r.status_code >= 400:
        raise BrokerCallFailed(f"broker {r.status_code}: {_short(r.text)}")
    return r.json()


def _chat_payload(messages: list[dict[str, Any]], max_tokens: int, temperature: float,
                  workflow: str | None, model: str | None,
                  response_format: dict[str, Any] | None) -> dict[str, Any]:
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
    return payload


def _chat_meta(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": data.get("provider"),
        "model": data.get("model"),
        "tokens_in": data.get("tokens_in", 0),
        "tokens_out": data.get("tokens_out", 0),
        "cost_usd": data.get("cost_usd", 0.0),
        "latency_ms": data.get("latency_ms", 0),
        "request_id": data.get("request_id"),
        "key_label": data.get("key_label"),
    }


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
    payload = _chat_payload(messages, max_tokens, temperature, workflow, model,
                            response_format)
    data = await _post(await _client(), "/v1/chat", {"capability": capability}, payload)
    meta = _chat_meta(data)
    await _log_usage(meta, workflow, event_id, capability)
    return data.get("text", ""), meta


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
    poll_deadline_s: float | None = None,
    resume_job_id: int | str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Submit+poll against /v1/jobs — same request/response contract as
    chat_via_broker, but never holds the connection open. A slow provider can
    only delay the poll loop, not 504 the caller. See docs/llm-broker.md.

    `poll_deadline_s` — per-call ceiling override (default env
    BROKER_JOB_DEADLINE_S): фоновые задачи (ярлыки кластеров) могут ждать
    занятый free-пул дольше интерактивных.
    `resume_job_id` — джоба, оставшаяся у брокера после прошлого
    BrokerJobPending: опросить её, а не отправлять payload заново.
    """
    payload = _chat_payload(messages, max_tokens, temperature, workflow, model,
                            response_format)
    ceiling = poll_deadline_s if poll_deadline_s else JOB_POLL_DEADLINE_S
    c = await _client()

    # Возобновление: джоба уже была отправлена в прошлый раз, и клиент
    # сдался по своему дедлайну, а брокер её досчитывал. Опрашиваем ЕЁ, а не
    # шлём заново — иначе модель считает то же самое второй раз (12.09.2026:
    # три vision-джобы по 25 минут в очереди брокера, все три пересчитаны).
    # 404 = брокер уже удалил результат (ретенция) — тогда честная переотправка.
    # Так же и `error`: возобновлять стоит только живую джобу. Упавшая
    # старая джоба отдавала бы свой давний «no provider» мгновенно и на КАЖДОЙ
    # попытке — 13.09.2026 одна такая (484699) дважды открыла circuit vision
    # на 30 минут для всех фото. Её провал уже учтён, вердикт выносит свежая.
    if resume_job_id is not None:
        try:
            return await _poll_job(c, resume_job_id, ceiling=ceiling, poll_after_s=2.0,
                                   workflow=workflow, event_id=event_id,
                                   capability=capability)
        except (BrokerJobGone, BrokerJobErrored) as e:
            log.info("job %s not resumable (%s) — resubmitting", resume_job_id, e)

    job = await _post(c, "/v1/jobs", {"capability": capability}, payload)
    return await _poll_job(c, job["job_id"], ceiling=ceiling,
                           poll_after_s=float(job.get("poll_after_s") or 2),
                           workflow=workflow, event_id=event_id, capability=capability)


async def _poll_job(c: httpx.AsyncClient, job_id: int | str, *, ceiling: float,
                    poll_after_s: float, workflow: str | None, event_id: int | None,
                    capability: str) -> tuple[str, dict[str, Any]]:
    data = await poll_job(c, BROKER_URL, job_id, ceiling=ceiling, poll_after_s=poll_after_s)
    meta = _chat_meta(data)
    await _log_usage(meta, workflow, event_id, capability)
    return data.get("text", ""), meta


async def embed_via_broker(texts: str | list[str]) -> list[list[float]]:
    """Drop-in replacement for the legacy embed()."""
    inputs = [texts] if isinstance(texts, str) else list(texts)
    data = await _post(await _client(), "/v1/embed", {"provider": "voyage"},
                       {"input": inputs})
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
