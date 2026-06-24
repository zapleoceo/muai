"""LLM client wrapper — единая точка входа для всех AI-вызовов.

Делает:
1. Подбирает провайдера из routing policy (free-first)
2. Берёт available token из repository
3. **Атомарно резервирует cost** перед paid вызовом (закрывает TOCTOU)
4. Делает HTTP вызов через singleton AsyncClient (connection pool)
5. Записывает usage_log + settle cost (actual - reserved)
6. Handles 429/cooldown automatically
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Literal

import httpx

from vera_shared.db.engine import get_session
from vera_shared.db.models import UsageLogRow
from vera_shared.llm.cost_guard import (
    DailyBudgetExceeded,
    estimate_cost,
    global_cost_today,
    global_daily_cap_from_env,
    invalidate_global_cost_cache,
)
from vera_shared.llm.registry import (
    PROVIDER_BASE_URL,
    PROVIDER_MODEL,
    PROVIDER_TIER,
    cost_usd,
    supports_json_schema,
)
from vera_shared.llm.routing import Capability, RoutingPolicy
from vera_shared.tokens import repository as token_repo
from vera_shared.tokens.model import Token

log = logging.getLogger(__name__)


class LLMCallFailed(Exception):
    """Все провайдеры в цепочке отказали."""


# ─── HTTP singleton ─────────────────────────────────────────────────────────


_http_client: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def _get_http_client() -> httpx.AsyncClient:
    """Module-level httpx singleton — переиспользует TCP/TLS connections.

    Без этого: 3 реплики × 27k событий × нов. TCP+TLS handshake каждый =
    тысячи открытых соединений и медленный latency.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        async with _http_lock:
            if _http_client is None or _http_client.is_closed:
                limits = httpx.Limits(
                    max_keepalive_connections=50,
                    max_connections=100,
                    keepalive_expiry=60.0,
                )
                _http_client = httpx.AsyncClient(timeout=60.0, limits=limits)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ─── Main chat ──────────────────────────────────────────────────────────────


async def chat(
    messages: list[dict[str, Any]],
    *,
    capability: Capability = "chat:fast",
    require_json_schema: bool = False,
    response_format: dict | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    workflow: str | None = None,
    event_id: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Chat-completion с автоматической ротацией и atomic cost reservation.

    BROKER_URL set → отдаём всё в aibroker (он сам делает chain+cost+rotation).
    Иначе — legacy путь по локальным tokens.
    """
    from vera_shared.llm.broker_client import (
        BrokerCallFailed, broker_enabled, chat_via_broker,
    )
    if broker_enabled():
        try:
            return await chat_via_broker(
                messages=messages, capability=capability,
                response_format=response_format,
                max_tokens=max_tokens, temperature=temperature,
                workflow=workflow, event_id=event_id,
            )
        except BrokerCallFailed as e:
            log.warning("broker call failed, falling back to local: %s", e)
            # fall-through to legacy path below

    # ── legacy: pick local tokens, walk chain ourselves ─────────────────
    chain = RoutingPolicy.chain_for(capability, require_json_schema=require_json_schema)
    last_error: Exception | None = None
    global_cap = global_daily_cap_from_env()

    for choice in chain:
        provider = choice.provider
        tokens = await token_repo.list_for_provider(provider)
        available = [t for t in tokens if t.is_available]
        if not available:
            continue
        # LRU + jitter — несколько реплик не выбирают одного и того же victim'a
        available.sort(key=lambda t: (t.last_used_at or datetime.min, random.random()))

        for tk in available:
            char_count = sum(len(str(m.get("content", ""))) for m in messages)
            t_in_est = max(100, char_count // 4)
            t_out_est = max_tokens
            est_cost = estimate_cost(PROVIDER_MODEL[provider], t_in_est, t_out_est)

            reserved = 0.0
            # ── BILLABLE: cap по реальной цене модели, НЕ по метке tier ──
            # Урок $20-burn: Gemini-ключ с tier="free", но включённым биллингом
            # в Google → при превышении free-квоты молча списывает деньги (без
            # 429). Любой вызов с est_cost>0 — billable и обязан пройти под cap.
            billable = est_cost > 0
            if billable:
                # Глобальный дневной cap — на ЛЮБОЙ billable вызов (free/trial тоже).
                if global_cap is not None:
                    g_used = await global_cost_today()
                    if g_used + est_cost > global_cap:
                        log.warning(
                            "Skip %s/%s — global cap: used=$%.4f + est=$%.4f > $%.2f",
                            provider, tk.label, g_used, est_cost, global_cap,
                        )
                        continue
                # Per-key атомарный резерв — для paid или ключей с явным cap.
                if tk.tier == "paid" or tk.daily_cost_cap_usd is not None:
                    cap = tk.daily_cost_cap_usd or 0.0
                    ok = await token_repo.reserve_paid_cost(
                        tk.id,
                        estimated_cost=est_cost,
                        daily_cap=cap,
                        monthly_cap=tk.monthly_cost_cap_usd,
                    )
                    if not ok:
                        log.warning("Skip %s/%s — token cap reservation failed",
                                    provider, tk.label)
                        continue
                    reserved = est_cost

            try:
                text_out, meta = await _call_provider(
                    provider, tk, messages,
                    max_tokens=max_tokens, temperature=temperature,
                    response_format=response_format,
                )
            except _RetryableError as e:
                # Возврат резерва + cooldown
                if reserved > 0:
                    await token_repo.release_reservation(tk.id, reserved_cost=reserved)
                log.warning("%s/%s retryable: %s", provider, tk.label, _scrub(str(e)))
                await token_repo.mark_cooldown(tk.id, seconds=60)
                last_error = e
                continue
            except Exception as e:
                if reserved > 0:
                    await token_repo.release_reservation(tk.id, reserved_cost=reserved)
                log.warning("%s/%s failed: %s", provider, tk.label, _scrub(str(e)))
                last_error = e
                continue

            # Success — settle и log
            actual_cost = cost_usd(meta["model"], meta["tokens_in"], meta["tokens_out"])

            if reserved > 0:
                await token_repo.record_paid_settled(
                    tk.id, actual_cost=actual_cost, reserved_cost=reserved,
                )
                invalidate_global_cost_cache()
            elif billable:
                # Billable без per-key cap (free/trial-метка с реальной ценой):
                # учитываем стоимость, чтобы глобальный cap её видел.
                await token_repo.record_usage(
                    tk.id, tokens_in=meta["tokens_in"],
                    tokens_out=meta["tokens_out"], cost_usd=actual_cost,
                )
                invalidate_global_cost_cache()
            else:
                await token_repo.record_free_usage(tk.id)

            await _log_usage(
                token_id=tk.id, provider=provider, model=meta["model"],
                capability=capability, tokens_in=meta["tokens_in"],
                tokens_out=meta["tokens_out"], cost_usd=actual_cost,
                latency_ms=meta["latency_ms"], success=True,
                workflow=workflow, event_id=event_id,
            )
            meta["provider"] = provider
            meta["token_label"] = tk.label
            meta["cost_usd"] = actual_cost
            return text_out, meta

    raise LLMCallFailed(f"All providers exhausted. Last: {_scrub(str(last_error))}")


def _scrub(s: str) -> str:
    """Убрать sk-... / Bearer ... из строк ошибок (на случай если провайдер
    echoes наш header в response)."""
    import re
    s = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "sk-***", s)
    s = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}", "Bearer ***", s)
    return s[:300]


class _RetryableError(Exception):
    pass


# OpenAI-compat providers — same wire format, different base_url + model.
_OPENAI_COMPAT_PROVIDERS = frozenset({
    "cerebras", "groq", "openrouter", "deepseek", "openai",
    "sambanova", "nvidia", "mistral",
})


def _check_http_status(r, t0: float) -> int:
    """Common: convert HTTP response into either ms-latency or a raised error.

    Raises _RetryableError on 429 / 5xx so the chain advances.
    Raises plain Exception on other non-200 (auth, bad req — caller skips key).
    """
    latency_ms = int((time.time() - t0) * 1000)
    if r.status_code == 429:
        raise _RetryableError(f"429: {r.text[:120]}")
    if r.status_code >= 500:
        raise _RetryableError(f"{r.status_code}: {r.text[:120]}")
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
    return latency_ms


async def _call_provider(
    provider: str,
    token: Token,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    response_format: dict | None,
) -> tuple[str, dict[str, Any]]:
    """OpenAI-compatible HTTP call."""
    base_url = PROVIDER_BASE_URL[provider]
    model = PROVIDER_MODEL[provider]
    client = await _get_http_client()

    # OpenAI-compat path
    if provider in _OPENAI_COMPAT_PROVIDERS:
        url = f"{base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        t0 = time.time()
        r = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {token.token}"},
        )
        latency_ms = _check_http_status(r, t0)

        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise _RetryableError(f"empty choices: {str(data)[:200]}")
        text_out = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return text_out, {
            "model": model,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": latency_ms,
        }

    # Gemini native API (НЕ OpenAI-compatible)
    if provider == "gemini":
        url = f"{base_url}/models/{model}:generateContent"
        # system → system_instruction, user → user, assistant → model
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        chat_msgs = [m for m in messages if m.get("role") != "system"]
        contents = [{"role": "user" if m["role"] == "user" else "model",
                     "parts": [{"text": m["content"]}]} for m in chat_msgs]
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if sys_msgs:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n".join(m["content"] for m in sys_msgs)}],
            }
        if response_format and response_format.get("type") == "json_schema":
            payload["generationConfig"]["responseMimeType"] = "application/json"
        elif response_format and response_format.get("type") == "json_object":
            payload["generationConfig"]["responseMimeType"] = "application/json"

        t0 = time.time()
        r = await client.post(url, json=payload, headers={"x-goog-api-key": token.token})
        latency_ms = _check_http_status(r, t0)

        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text_out = "".join(p.get("text", "") for p in parts).strip()
        usage = data.get("usageMetadata", {})
        return text_out, {
            "model": model,
            "tokens_in": usage.get("promptTokenCount", 0),
            "tokens_out": usage.get("candidatesTokenCount", 0),
            "latency_ms": latency_ms,
        }

    # Anthropic Messages API (НЕ OpenAI-compatible)
    if provider == "anthropic":
        url = f"{base_url}/v1/messages"
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        chat_msgs = [m for m in messages if m.get("role") != "system"]
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": m["role"], "content": m["content"]}
                          for m in chat_msgs],
        }
        if sys_msgs:
            payload["system"] = "\n".join(m["content"] for m in sys_msgs)
        # Anthropic does not honour OpenAI's response_format; if caller wanted
        # JSON we add a hint to the system prompt.
        if response_format and response_format.get("type", "").startswith("json"):
            payload["system"] = (payload.get("system", "") +
                                  "\n\nIMPORTANT: respond with valid JSON only.").strip()

        t0 = time.time()
        r = await client.post(
            url, json=payload,
            headers={
                "x-api-key": token.token,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        latency_ms = _check_http_status(r, t0)

        data = r.json()
        blocks = data.get("content") or []
        text_out = "".join(b.get("text", "") for b in blocks
                            if b.get("type") == "text").strip()
        usage = data.get("usage", {})
        return text_out, {
            "model": model,
            "tokens_in": usage.get("input_tokens", 0),
            "tokens_out": usage.get("output_tokens", 0),
            "latency_ms": latency_ms,
        }

    raise NotImplementedError(f"Unknown provider: {provider}")


async def _log_usage(**kwargs) -> None:
    async with get_session() as s:
        row = UsageLogRow(**kwargs)
        s.add(row)


# ─── Embedder ───────────────────────────────────────────────────────────────


async def embed(text: str | list[str]) -> list[list[float]]:
    """Voyage embedding с ротацией ключей.

    ВАЖНО: возвращает list[list[float]] — даже если на вход str, оборачиваем
    в [text] (НЕ итерируем по char).

    BROKER_URL set → отдаём всё в aibroker. Иначе — legacy.
    """
    from vera_shared.llm.broker_client import (
        BrokerCallFailed, broker_enabled, embed_via_broker,
    )
    if broker_enabled():
        try:
            return await embed_via_broker(text)
        except BrokerCallFailed as e:
            log.warning("broker embed failed, falling back to local: %s", e)
            # fall-through to legacy

    if isinstance(text, str):
        items: list[str] = [text]
    else:
        items = list(text)
    if not items:
        return []
    tokens = await token_repo.list_for_provider("voyage")
    available = [t for t in tokens if t.is_available]
    if not available:
        raise LLMCallFailed("No voyage tokens available")
    available.sort(key=lambda t: (t.last_used_at or datetime.min, random.random()))

    client = await _get_http_client()
    last_error: Exception | None = None
    for tk in available:
        try:
            t0 = time.time()
            r = await client.post(
                "https://api.voyageai.com/v1/embeddings",
                json={"model": "voyage-3", "input": items},
                headers={"Authorization": f"Bearer {tk.token}"},
                timeout=30.0,
            )
            if r.status_code == 429:
                await token_repo.mark_cooldown(tk.id, seconds=30)
                continue
            if r.status_code != 200:
                last_error = Exception(f"voyage HTTP {r.status_code}: {r.text[:120]}")
                continue
            data = r.json()
            vectors = [d["embedding"] for d in data["data"]]
            usage = data.get("usage", {}) or {}
            tokens_in = usage.get("total_tokens") or sum(max(1, len(s) // 4) for s in items)
            await token_repo.record_free_usage(tk.id)
            return vectors
        except Exception as e:
            last_error = e
            continue

    raise LLMCallFailed(f"All voyage tokens failed. Last: {_scrub(str(last_error))}")
