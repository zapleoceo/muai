"""LiteLLM router built from our SQLite tokens table at startup."""
import asyncio
import logging
import os
from datetime import datetime

import litellm
from litellm import Router

from vera_shared.tokens import repository as token_repo

log = logging.getLogger(__name__)

# LiteLLM model alias → list of underlying providers/keys.
# Aliases used in code: "chat:fast", "chat:smart", "chat:vision".
# Built dynamically from the tokens table at app startup.

_router: Router | None = None
_lock = asyncio.Lock()

# Per-provider model name + LiteLLM provider prefix.
_PROVIDER_MODEL: dict[str, tuple[str, str]] = {
    # provider → (litellm_provider, model)
    "gemini":    ("gemini",      "gemini-flash-lite-latest"),
    "deepseek":  ("deepseek",    "deepseek-chat"),
    "anthropic": ("anthropic",   "claude-haiku-4-5"),
}

# Map our capability tag → list of providers in fallback order.
_CAPABILITY_ORDER = {
    "chat:fast":  ["gemini", "deepseek", "anthropic"],
    "prefilter":  ["gemini", "deepseek", "anthropic"],
    "chat:smart": ["anthropic", "deepseek", "gemini"],
    "chat:code":  ["anthropic", "deepseek", "gemini"],
}


async def _build_model_list() -> list[dict]:
    rows = await token_repo.get_all_active()
    out: list[dict] = []
    for r in rows:
        info = _PROVIDER_MODEL.get(r.provider)
        if info is None:
            continue
        provider_prefix, base_model = info
        out.append({
            "model_name": f"chat:fast::{r.provider}",  # group alias
            "litellm_params": {
                "model": f"{provider_prefix}/{base_model}",
                "api_key": r.token,
                "rpm": 60,  # LiteLLM throttle hint; not enforced strictly
            },
            "model_info": {"db_token_id": r.id, "provider": r.provider},
        })
    return out


async def _get_router() -> Router:
    global _router
    if _router is not None:
        return _router
    async with _lock:
        if _router is not None:
            return _router
        model_list = await _build_model_list()
        if not model_list:
            raise RuntimeError("No active tokens for any LLM provider")

        # Build fallbacks list per logical alias
        fallbacks = []
        for capability, providers in _CAPABILITY_ORDER.items():
            fallbacks.append({
                capability: [f"chat:fast::{p}" for p in providers
                             if any(m["model_info"]["provider"] == p for m in model_list)],
            })

        # Set litellm to drop unknown params and not crash on provider quirks
        litellm.drop_params = True

        _router = Router(
            model_list=model_list,
            fallbacks=fallbacks,
            num_retries=2,
            retry_after=5,
            timeout=60,
            routing_strategy="usage-based-routing-v2",
        )

        # Hook usage callback so we update our tokens table
        litellm.success_callback = [_on_success]
        litellm.failure_callback = [_on_failure]

        log.info("LiteLLM router initialised with %d keys across %d providers",
                 len(model_list), len({m["model_info"]["provider"] for m in model_list}))
    return _router


def _on_success(kwargs, completion_response, start_time, end_time):
    """LiteLLM sync callback. Persist usage to our tokens table."""
    try:
        token_id = (
            kwargs.get("litellm_params", {}).get("model_info", {}).get("db_token_id")
        )
        if not token_id:
            return
        usage = (completion_response or {}).get("usage", {}) or {}
        t_in = usage.get("prompt_tokens", 0) or 0
        t_out = usage.get("completion_tokens", 0) or 0
        cost = kwargs.get("response_cost") or 0.0
        # Schedule async write — we're in a sync callback context
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(token_repo.record_usage(token_id, t_in, t_out, float(cost)))
    except Exception as exc:
        log.warning("usage callback failed: %s", exc)


def _on_failure(kwargs, completion_response, start_time, end_time):
    try:
        token_id = (
            kwargs.get("litellm_params", {}).get("model_info", {}).get("db_token_id")
        )
        exc = kwargs.get("exception")
        if token_id and exc:
            log.warning("LiteLLM call failed for token %d: %s", token_id, exc)
    except Exception:
        pass


async def chat(
    messages: list[dict],
    system: str | None = None,
    capability: str = "chat:fast",
    **extra_kwargs,
) -> str:
    """High-level: returns just the assistant text content."""
    text, _ = await chat_with_meta(messages, system=system, capability=capability, **extra_kwargs)
    return text


async def chat_with_meta(
    messages: list[dict],
    system: str | None = None,
    capability: str = "chat:fast",
    **extra_kwargs,
) -> tuple[str, dict]:
    """Returns (text, meta) where meta has model, usage, cost_usd."""
    router = await _get_router()
    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    response = await router.acompletion(
        model=capability,
        messages=msgs,
        **extra_kwargs,
    )
    text = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    meta = {
        "model": getattr(response, "model", capability),
        "tokens_in": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "tokens_out": getattr(usage, "completion_tokens", 0) if usage else 0,
        "cost_usd": float(getattr(response, "_hidden_params", {}).get("response_cost", 0) or 0),
    }
    return text, meta


async def reset_router() -> None:
    """Force-rebuild router after tokens table changed (added/removed key)."""
    global _router
    async with _lock:
        _router = None
