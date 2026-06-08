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

# All routing config now lives in vera_shared.llm.registry — single source of
# truth shared with cost_guard, pool_clients, multimodal.
from vera_shared.llm.registry import (
    PROVIDER_MODEL as _PROVIDER_MODEL_NAME,
    CAPABILITY_ORDER as _CAPABILITY_ORDER,
    is_paid as _is_paid,
)


def _provider_prefix_and_model(provider: str) -> tuple[str, str] | None:
    """LiteLLM expects `{prefix}/{model}` for routing.
    Prefix == provider for all our providers except openrouter (handled
    separately because it's OpenAI-compatible)."""
    model = _PROVIDER_MODEL_NAME.get(provider)
    if model is None:
        return None
    return (provider, model)


async def _build_model_list() -> list[dict]:
    rows = await token_repo.get_all_active()
    out: list[dict] = []
    for r in rows:
        info = _provider_prefix_and_model(r.provider)
        if info is None:
            continue
        provider_prefix, base_model = info
        # OpenRouter/Cerebras/Groq are all OpenAI-compatible — LiteLLM
        # needs explicit api_base + custom_llm_provider="openai" so it
        # doesn't try to look them up as native providers it doesn't ship.
        params: dict = {
            "model": f"{provider_prefix}/{base_model}",
            "api_key": r.token,
        }
        if r.provider == "openrouter":
            params["model"] = base_model
            params["api_base"] = "https://openrouter.ai/api/v1"
            params["custom_llm_provider"] = "openai"
        elif r.provider == "cerebras":
            params["model"] = base_model
            params["api_base"] = "https://api.cerebras.ai/v1"
            params["custom_llm_provider"] = "openai"
        elif r.provider == "groq":
            # Groq's model id literally is "openai/gpt-oss-120b" — but
            # LiteLLM with custom_llm_provider="openai" auto-strips the
            # "openai/" prefix, so we get sent as plain "gpt-oss-120b"
            # which Groq doesn't have. Double-prefix workaround: LiteLLM
            # strips the outer "openai/" and forwards "openai/gpt-oss-120b"
            # which is what Groq actually expects.
            params["model"] = f"openai/{base_model}"
            params["api_base"] = "https://api.groq.com/openai/v1"
            params["custom_llm_provider"] = "openai"
        is_paid = _is_paid(r.provider, r.label)
        # Weight tuning: free pool burns first, paid is safety net.
        # weight=20 paid vs 100 free → paid sees ~17% of normal traffic,
        # but LiteLLM falls through to it instantly when free returns 429.
        params["weight"] = 20 if is_paid else 100
        out.append({
            "model_name": f"chat:fast::{r.provider}",  # group alias
            "litellm_params": params,
            "model_info": {"db_token_id": r.id, "provider": r.provider,
                           "label": r.label, "is_paid": is_paid},
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
            # simple-shuffle respects per-deployment `weight`. Paid keys
            # get weight=1, free get weight=100 → paid is touched ~1% of
            # the time only, when free pool got picked-around already.
            routing_strategy="simple-shuffle",
        )

        # Hook usage callback so we update our tokens table
        litellm.success_callback = [_on_success]
        litellm.failure_callback = [_on_failure]

        log.info("LiteLLM router initialised with %d keys across %d providers",
                 len(model_list), len({m["model_info"]["provider"] for m in model_list}))
    return _router


def _on_success(kwargs, completion_response, start_time, end_time):
    """LiteLLM sync callback. Persist usage to our tokens table.

    NOTE: we IGNORE LiteLLM's response_cost / kwargs.response_cost — it
    uses a stale pricing table that under-counts new models (gemini-3.5-flash
    was reported at 2.5-flash pricing = 20× under-count, which caused the
    $25 burn incident on 2026-06-01). Instead we recompute cost from
    actual token usage against our hand-maintained price table in cost_guard.
    """
    try:
        model_info = (
            kwargs.get("model_info")
            or (kwargs.get("litellm_params") or {}).get("model_info")
            or {}
        )
        token_id = model_info.get("db_token_id")
        if not token_id:
            return
        usage = (completion_response or {}).get("usage", {}) or {}
        t_in = usage.get("prompt_tokens", 0) or 0
        t_out = usage.get("completion_tokens", 0) or 0
        model = kwargs.get("model") or ""
        from vera_shared.llm.cost_guard import estimate_cost
        cost = estimate_cost(model, t_in, t_out)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(token_repo.record_usage(token_id, t_in, t_out, cost))
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
    """Returns (text, meta) where meta has model, usage, cost_usd.

    Wrapped in cost_guard.check_and_reserve so a runaway loop can't burn
    through the daily LLM budget. The guard uses prompt-length heuristics
    BEFORE the call; actual cost is recorded post-call in _on_success.
    """
    router = await _get_router()
    msgs = list(messages)
    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    # Pre-flight cost gate. Heuristic estimate: 1 token ≈ 4 chars.
    # Output estimate caps at max_tokens (if provided) or 2k.
    # Uses the most expensive plausible model so the gate is conservative —
    # if it would push us over the cap on the worst case, refuse.
    from vera_shared.llm.cost_guard import check_and_reserve, DailyBudgetExceeded
    from vera_shared.llm.registry import PROVIDER_MODEL
    char_count = sum(len(str(m.get("content", ""))) for m in msgs)
    t_in_est = max(1, char_count // 4)
    t_out_est = int(extra_kwargs.get("max_tokens", 2000) or 2000)
    # Default to gemini model since most chat:fast traffic flows through it.
    # Registry change → new model name picked up automatically.
    estimate_model = PROVIDER_MODEL.get("gemini", "gemini-2.5-flash")
    try:
        await check_and_reserve(estimate_model, t_in_est, t_out_est)
    except DailyBudgetExceeded as exc:
        log.warning("chat() refused: %s", exc)
        raise

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
