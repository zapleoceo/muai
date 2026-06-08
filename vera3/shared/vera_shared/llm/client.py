"""LLM client wrapper — единая точка входа для всех AI-вызовов.

Делает:
1. Подбирает провайдера из routing policy (free-first)
2. Берёт available token из repository
3. Проверяет cost cap перед paid вызовом
4. Делает HTTP вызов (OpenAI-compatible)
5. Записывает usage_log + обновляет token counters
6. Handles 429/cooldown automatically
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Literal

import httpx

from vera_shared.db.engine import get_session
from vera_shared.db.models import UsageLogRow
from vera_shared.llm.cost_guard import (
    DailyBudgetExceeded,
    assert_can_call_paid,
    estimate_cost,
    global_daily_cap_from_env,
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


class _GlobalCostTracker:
    """In-memory + DB sync счётчик глобальных расходов за день."""
    def __init__(self) -> None:
        self._day = datetime.utcnow().date()
        self._cost_today = 0.0

    def add(self, cost: float) -> None:
        today = datetime.utcnow().date()
        if today != self._day:
            self._day = today
            self._cost_today = 0.0
        self._cost_today += cost

    @property
    def cost_today(self) -> float:
        return self._cost_today


_tracker = _GlobalCostTracker()


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
    """Сделать chat-completion вызов с автоматической ротацией.

    Returns:
        (text, meta) — где meta содержит provider, model, tokens, cost_usd, latency_ms
    """
    chain = RoutingPolicy.chain_for(capability, require_json_schema=require_json_schema)
    last_error: Exception | None = None
    global_cap = global_daily_cap_from_env()

    for choice in chain:
        provider = choice.provider
        tokens = await token_repo.list_for_provider(provider)
        available = [t for t in tokens if t.is_available]
        if not available:
            continue
        available.sort(key=lambda t: t.last_used_at or datetime.min)

        for tk in available:
            # Pre-flight cost check
            char_count = sum(len(str(m.get("content", ""))) for m in messages)
            t_in_est = max(100, char_count // 4)
            t_out_est = max_tokens
            est_cost = estimate_cost(PROVIDER_MODEL[provider], t_in_est, t_out_est)

            try:
                assert_can_call_paid(
                    tk.tier,
                    tk.daily_cost_used_usd,
                    tk.daily_cost_cap_usd,
                    est_cost,
                    global_daily_used=_tracker.cost_today,
                    global_daily_cap=global_cap,
                )
            except DailyBudgetExceeded as e:
                log.warning("Skip %s/%s — cap exceeded: %s", provider, tk.label, e)
                continue

            try:
                text, meta = await _call_provider(
                    provider, tk, messages,
                    max_tokens=max_tokens, temperature=temperature,
                    response_format=response_format,
                )
            except _RetryableError as e:
                log.warning("%s/%s retryable: %s", provider, tk.label, e)
                await token_repo.mark_cooldown(tk.id, seconds=60)
                last_error = e
                continue
            except Exception as e:
                log.warning("%s/%s failed: %s", provider, tk.label, e)
                last_error = e
                continue

            # Success — record
            actual_cost = cost_usd(
                meta["model"], meta["tokens_in"], meta["tokens_out"]
            )
            await token_repo.record_usage(
                tk.id, tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
                cost_usd=actual_cost,
            )
            _tracker.add(actual_cost)
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
            return text, meta

    raise LLMCallFailed(f"All providers exhausted. Last: {last_error}")


class _RetryableError(Exception):
    pass


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

    # OpenAI-compat path
    if provider in {"cerebras", "groq", "openrouter", "deepseek", "openai", "sambanova", "nvidia", "mistral"}:
        url = f"{base_url}/chat/completions"
        # Groq model уже содержит "openai/" префикс в registry — НЕ удваиваем
        api_model = model
        payload: dict[str, Any] = {
            "model": api_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        t0 = time.time()
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {token.token}"},
            )
        latency_ms = int((time.time() - t0) * 1000)

        if r.status_code == 429:
            raise _RetryableError(f"429: {r.text[:120]}")
        if r.status_code >= 500:
            raise _RetryableError(f"{r.status_code}: {r.text[:120]}")
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        return text, {
            "model": model,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": latency_ms,
        }

    # Gemini native API (НЕ OpenAI-compatible)
    if provider == "gemini":
        url = f"{base_url}/models/{model}:generateContent"
        contents = [{"role": "user" if m["role"] == "user" else "model",
                     "parts": [{"text": m["content"]}]} for m in messages]
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if response_format and response_format.get("type") == "json_schema":
            # Gemini не принимает наш OpenAI-стиль schema (другие field names).
            # Просим просто JSON output без strict schema.
            payload["generationConfig"]["responseMimeType"] = "application/json"

        t0 = time.time()
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(url, json=payload, headers={"x-goog-api-key": token.token})
        latency_ms = int((time.time() - t0) * 1000)

        if r.status_code == 429:
            raise _RetryableError(f"429: {r.text[:120]}")
        if r.status_code >= 500:
            raise _RetryableError(f"{r.status_code}: {r.text[:120]}")
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        usage = data.get("usageMetadata", {})
        return text, {
            "model": model,
            "tokens_in": usage.get("promptTokenCount", 0),
            "tokens_out": usage.get("candidatesTokenCount", 0),
            "latency_ms": latency_ms,
        }

    raise NotImplementedError(f"Unknown provider: {provider}")


async def _log_usage(**kwargs) -> None:
    async with get_session() as s:
        row = UsageLogRow(**kwargs)
        s.add(row)


# ─── Embedder ───────────────────────────────────────────────────────────────


async def embed(text: str | list[str]) -> list[list[float]]:
    """Voyage embedding с ротацией ключей."""
    items = [text] if isinstance(text, str) else list(text)
    tokens = await token_repo.list_for_provider("voyage")
    available = [t for t in tokens if t.is_available]
    if not available:
        raise LLMCallFailed("No voyage tokens available")
    available.sort(key=lambda t: t.last_used_at or datetime.min)

    last_error: Exception | None = None
    for tk in available:
        try:
            t0 = time.time()
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    "https://api.voyageai.com/v1/embeddings",
                    json={"model": "voyage-3", "input": items},
                    headers={"Authorization": f"Bearer {tk.token}"},
                )
            if r.status_code == 429:
                await token_repo.mark_cooldown(tk.id, seconds=30)
                continue
            if r.status_code != 200:
                last_error = Exception(f"voyage HTTP {r.status_code}: {r.text[:120]}")
                continue
            data = r.json()
            vectors = [d["embedding"] for d in data["data"]]
            # crude token estimate: ~4 chars per token
            tokens_in = sum(max(1, len(s) // 4) for s in items)
            await token_repo.record_usage(tk.id, tokens_in=tokens_in, tokens_out=0, cost_usd=0.0)
            return vectors
        except Exception as e:
            last_error = e
            continue

    raise LLMCallFailed(f"All voyage tokens failed. Last: {last_error}")
