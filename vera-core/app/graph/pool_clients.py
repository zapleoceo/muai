"""Graphiti LLM/Embedder/Reranker clients that rotate keys via TokenPool.

Model selection: pulls from vera_shared.llm.registry.PROVIDER_MODEL. Change
the model name THERE and every Graphiti path picks it up on next restart.

Key rotation strategy:
  - On 429: cooldown the key for _GEMINI_429_COOLDOWN seconds (65 — Gemini's
    per-minute RPM window resets at 60s + 5s safety).
  - On 503: cooldown _GEMINI_503_COOLDOWN seconds (300).
  - On 401/403: cooldown _GEMINI_AUTH_COOLDOWN seconds (3600 — actual hard fail).
  - On TokensExhausted: all Gemini keys unavailable — raise so caller skips the write.
  - No DeepSeek fallback for Graphiti calls: DeepSeek rejects response_format:json_schema.

Usage tracking: every successful call writes to tokens.daily_cost_used_usd
via token_repo.record_usage so per-key cost caps work everywhere.
"""

import logging

from vera_shared.tokens.pool import TokensExhausted, get_pool
from vera_shared.tokens.selector import get_token

log = logging.getLogger(__name__)

_GEMINI_429_COOLDOWN  = 65     # Gemini RPM window resets at 60s; +5s safety
_GEMINI_503_COOLDOWN  = 300
_GEMINI_AUTH_COOLDOWN = 3600   # 401/403 IS a hard fail — wait an hour
_MAX_KEY_ROTATIONS    = 5      # max distinct keys to try per call


def _status_from_exc(exc: Exception) -> int:
    cls = type(exc).__name__
    if cls in ("RateLimitError", "TokensExhausted"):
        return 429
    text = str(exc).lower()
    if ("429" in text or "quota" in text or "rate" in text
            or "resource_exhausted" in text or "rate limit" in text):
        return 429
    if "503" in text or "unavailable" in text or "high demand" in text:
        return 503
    if "401" in text or "403" in text or "permission" in text:
        return 401
    if "500" in text or "internal" in text:
        return 500
    return 0


# Per-token cache so we don't spin up a fresh aiohttp session every retry.
# Re-creating genai.Client(...) each call left stale connectors around; once
# the SDK's internal retry fired against the prior session we got
# `Connector is closed` / `Server disconnected` failures.
_GENAI_CLIENT_CACHE: dict[int, object] = {}


async def _refresh_gemini_client(holder, *, capability: str = "chat:fast"):
    from google import genai
    token = await get_token("gemini", capability)
    cached = _GENAI_CLIENT_CACHE.get(token.id)
    if cached is None:
        cached = genai.Client(api_key=token.token)
        _GENAI_CLIENT_CACHE[token.id] = cached
    holder.client = cached
    return token


# ── LLM ──────────────────────────────────────────────────────────────────────


def make_llm_client(model: str | None = None, capability: str = "chat:fast"):
    """Build Graphiti's GeminiClient with key rotation. Model defaults to
    whatever vera_shared.llm.registry.PROVIDER_MODEL says for 'gemini' —
    change the registry to upgrade everywhere."""
    from vera_shared.llm.registry import PROVIDER_MODEL
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

    if model is None:
        model = PROVIDER_MODEL.get("gemini", "gemini-2.5-flash")

    from vera_shared.llm.cost_guard import (
        DailyBudgetExceeded, check_and_reserve, estimate_cost,
    )
    from vera_shared.tokens import repository as token_repo

    # Heuristic token estimates for cost-guard. Graphiti calls have variable
    # input sizes (retrieved context); we use a conservative upper bound.
    _PROMPT_EST = 8000   # avg input tokens per Graphiti LLM call
    _OUTPUT_EST = 2000   # max output we'd typically see

    class PoolGeminiClient(GeminiClient):
        async def _generate_response(self, *args, **kwargs):
            # Hard cost-ceiling: refuse before sending if it would push
            # 24h spend over VERA_DAILY_LIMIT_USD (default $1).
            try:
                await check_and_reserve(model, _PROMPT_EST, _OUTPUT_EST)
            except DailyBudgetExceeded as exc:
                log.warning("Graphiti LLM: daily budget exceeded — %s", exc)
                raise

            last_exc: Exception | None = None
            for attempt in range(_MAX_KEY_ROTATIONS):
                try:
                    token = await _refresh_gemini_client(self, capability=capability)
                except TokensExhausted as exc:
                    log.warning(
                        "Graphiti LLM: all Gemini keys exhausted (attempt %d) — "
                        "episode write skipped",
                        attempt,
                    )
                    raise

                try:
                    response = await super()._generate_response(*args, **kwargs)
                    # Record real usage from the response (Google SDK populates
                    # usage_metadata on the holder.client after the call).
                    try:
                        meta = getattr(getattr(self, "client", None), "_last_usage", None)
                        if meta is None:
                            # Fallback: heuristic estimates so we at least see something
                            tin, tout = _PROMPT_EST, _OUTPUT_EST
                        else:
                            tin = int(meta.get("prompt_token_count", _PROMPT_EST) or 0)
                            tout = int(meta.get("candidates_token_count", _OUTPUT_EST) or 0)
                        cost = estimate_cost(model, tin, tout)
                        await token_repo.record_usage(token.id, tin, tout, cost)
                    except Exception as track_exc:
                        log.debug("usage tracking failed: %s", track_exc)
                    return response
                except TokensExhausted as exc:
                    log.warning("Graphiti LLM: TokensExhausted mid-call — rotating key")
                    last_exc = exc
                    continue
                except Exception as exc:
                    status = _status_from_exc(exc)
                    if status == 429:
                        await get_pool().on_error(
                            token.id, 429,
                            retry_after_seconds=_GEMINI_429_COOLDOWN,
                        )
                        log.warning(
                            "Graphiti LLM: Gemini key %d hit 429 — "
                            "cooling down 1h, rotating (attempt %d/%d)",
                            token.id, attempt + 1, _MAX_KEY_ROTATIONS,
                        )
                        last_exc = exc
                        continue
                    elif status == 503:
                        await get_pool().on_error(
                            token.id, 503,
                            retry_after_seconds=_GEMINI_503_COOLDOWN,
                        )
                        log.warning("Graphiti LLM: Gemini 503 — rotating key")
                        last_exc = exc
                        continue
                    elif status in (401, 403):
                        await get_pool().on_error(
                            token.id, status,
                            retry_after_seconds=_GEMINI_AUTH_COOLDOWN,
                        )
                    raise

            log.error("Graphiti LLM: exhausted %d Gemini key rotations", _MAX_KEY_ROTATIONS)
            raise last_exc or TokensExhausted("gemini", capability)

    return PoolGeminiClient(config=LLMConfig(api_key="placeholder", model=model))


# ── OpenAI-compatible providers (Cerebras, Groq, DeepSeek) ──────────────────


_OPENAI_POOL_CLIENT_CACHE: dict[tuple[str, int], object] = {}


def _make_openai_pool_client(
    *, provider: str, base_url: str, model: str, capability: str = "chat:fast",
):
    """Build a Graphiti LLM client backed by an OpenAI-compatible endpoint
    with our TokenPool rotation. Used for Cerebras, Groq, and similar
    providers that speak the OpenAI Chat Completions protocol.
    """
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from openai import AsyncOpenAI

    from vera_shared.llm.cost_guard import estimate_cost
    from vera_shared.tokens import repository as token_repo

    _PROMPT_EST = 8000
    _OUTPUT_EST = 2000

    async def _refresh(holder):
        token = await get_token(provider, capability)
        key = (provider, token.id)
        cached = _OPENAI_POOL_CLIENT_CACHE.get(key)
        if cached is None:
            cached = AsyncOpenAI(api_key=token.token, base_url=base_url)
            _OPENAI_POOL_CLIENT_CACHE[key] = cached
        holder.client = cached
        return token

    class PoolOpenAICompatClient(OpenAIGenericClient):
        async def _generate_response(self, *args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(_MAX_KEY_ROTATIONS):
                try:
                    token = await _refresh(self)
                except TokensExhausted as exc:
                    log.warning(
                        "Graphiti LLM: all %s keys exhausted (attempt %d)",
                        provider, attempt,
                    )
                    raise

                try:
                    response = await super()._generate_response(*args, **kwargs)
                    try:
                        cost = estimate_cost(model, _PROMPT_EST, _OUTPUT_EST)
                        await token_repo.record_usage(
                            token.id, _PROMPT_EST, _OUTPUT_EST, cost,
                        )
                    except Exception as track_exc:
                        log.debug("%s usage tracking failed: %s", provider, track_exc)
                    return response
                except TokensExhausted as exc:
                    last_exc = exc
                    continue
                except Exception as exc:
                    status = _status_from_exc(exc)
                    if status == 429:
                        await get_pool().on_error(token.id, 429, retry_after_seconds=_GEMINI_429_COOLDOWN)
                        log.warning("Graphiti LLM: %s key %d hit 429 — rotating",
                                    provider, token.id)
                        last_exc = exc
                        continue
                    elif status in (401, 403):
                        await get_pool().on_error(
                            token.id, status,
                            retry_after_seconds=_GEMINI_AUTH_COOLDOWN,
                        )
                    elif status == 503:
                        await get_pool().on_error(
                            token.id, 503,
                            retry_after_seconds=_GEMINI_503_COOLDOWN,
                        )
                        last_exc = exc
                        continue
                    raise

            raise last_exc or TokensExhausted(provider, capability)

    return PoolOpenAICompatClient(
        config=LLMConfig(api_key="placeholder", base_url=base_url, model=model),
    )


def make_cerebras_llm_client(capability: str = "chat:fast"):
    from vera_shared.llm.registry import PROVIDER_MODEL
    return _make_openai_pool_client(
        provider="cerebras",
        base_url="https://api.cerebras.ai/v1",
        model=PROVIDER_MODEL.get("cerebras", "llama-3.3-70b"),
        capability=capability,
    )


def make_groq_llm_client(capability: str = "chat:fast"):
    from vera_shared.llm.registry import PROVIDER_MODEL
    return _make_openai_pool_client(
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        model=PROVIDER_MODEL.get("groq", "llama-3.3-70b-versatile"),
        capability=capability,
    )


# ── Multi-provider wrapper: tries clients in order, falls through on fail ────


def make_multi_llm_client():
    """Cerebras first → Groq → Gemini fallback. Each level has its own
    pooled rotation of keys. We move to the next provider only when the
    current one is fully exhausted (all keys cooled down) or returns a
    structural error the SDK couldn't recover from.

    Why this order: Cerebras + Groq have orders-of-magnitude bigger quotas
    than Gemini (~5M tok/day × 5 keys for Cerebras vs 4500 req/day for
    Gemini free) AND much faster inference. Gemini is the precious-but-
    small resource we keep as last resort.
    """
    from graphiti_core.llm_client.client import LLMClient

    children = [
        make_cerebras_llm_client(),
        make_groq_llm_client(),
        make_llm_client(),  # Gemini
    ]

    class MultiProviderLLM(LLMClient):
        def __init__(self):
            # Take first child's config so Graphiti has something to read
            # (model name, temperature, etc.). We don't actually use it.
            super().__init__(config=children[0].config)
            self._children = children

        async def _generate_response(self, *args, **kwargs):
            last_exc: Exception | None = None
            for child in self._children:
                try:
                    return await child._generate_response(*args, **kwargs)
                except TokensExhausted as exc:
                    log.info(
                        "Graphiti LLM: %s exhausted, trying next provider",
                        type(child).__name__,
                    )
                    last_exc = exc
                    continue
                except Exception as exc:
                    # Non-pool error — log and try next anyway, since we'd
                    # rather complete with a different provider than fail.
                    log.warning(
                        "Graphiti LLM: %s raised %s — falling through",
                        type(child).__name__, type(exc).__name__,
                    )
                    last_exc = exc
                    continue
            raise last_exc or RuntimeError("All Graphiti LLM providers failed")

        async def generate_response(self, *args, **kwargs):
            # Delegate to the same fall-through chain via base class plumbing.
            return await super().generate_response(*args, **kwargs)

    return MultiProviderLLM()


# ── Embedder ──────────────────────────────────────────────────────────────────


def make_embedder(embedding_model: str = "voyage-3"):
    """Voyage embeddings — separate pool, never burns Gemini quota."""
    from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig

    async def _swap_voyage_client(holder):
        import voyageai
        tok = await get_token("voyage", "embed")
        holder.config.api_key = tok.token
        holder.client = voyageai.AsyncClient(api_key=tok.token)
        return tok

    # Per-call usage tracking — was missing, making Voyage spend invisible
    # (the same blind-spot bug as Graphiti's bypass of LiteLLM).
    from vera_shared.llm.registry import cost_usd as _cost_usd
    from vera_shared.tokens import repository as token_repo

    def _approx_tokens(input_data) -> int:
        """Voyage SDK doesn't return usage. Heuristic: 1 token ≈ 4 chars."""
        if isinstance(input_data, str):
            return max(1, len(input_data) // 4)
        if isinstance(input_data, list):
            return sum(max(1, len(str(s)) // 4) for s in input_data)
        return 1

    async def _track(tok, input_data):
        try:
            tin = _approx_tokens(input_data)
            cost = _cost_usd(embedding_model, tin, 0)
            await token_repo.record_usage(tok.id, tin, 0, cost)
        except Exception as exc:
            log.debug("voyage usage tracking failed: %s", exc)

    class PoolVoyageEmbedder(VoyageAIEmbedder):
        async def create(self, input_data):
            tok = await _swap_voyage_client(self)
            try:
                result = await super().create(input_data)
                await _track(tok, input_data)
                return result
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(tok.id, status)
                raise

        async def create_batch(self, input_data_list):
            tok = await _swap_voyage_client(self)
            try:
                result = await super().create_batch(input_data_list)
                await _track(tok, input_data_list)
                return result
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(tok.id, status)
                raise

    return PoolVoyageEmbedder(
        config=VoyageAIEmbedderConfig(api_key="placeholder",
                                      embedding_model=embedding_model),
    )


# ── Reranker ──────────────────────────────────────────────────────────────────


def make_reranker(model: str | None = None):
    """Reranker is a per-token relevance score; model defaults to whatever
    the registry says for the gemini provider."""
    from vera_shared.llm.registry import PROVIDER_MODEL, cost_usd as _cost_usd
    from vera_shared.tokens import repository as token_repo
    from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
    from graphiti_core.llm_client.config import LLMConfig

    if model is None:
        model = PROVIDER_MODEL.get("gemini", "gemini-2.5-flash")

    class PoolGeminiReranker(GeminiRerankerClient):
        async def rank(self, query, passages):
            token = await _refresh_gemini_client(self, capability="chat:fast")
            try:
                result = await super().rank(query, passages)
                # Approx tracking — reranker has no usage_metadata exposed.
                try:
                    tin = max(1, (len(query) + sum(len(str(p)) for p in passages)) // 4)
                    await token_repo.record_usage(
                        token.id, tin, 0, _cost_usd(model, tin, 0)
                    )
                except Exception as exc:
                    log.debug("reranker usage tracking failed: %s", exc)
                return result
            except Exception as exc:
                status = _status_from_exc(exc)
                if status == 429:
                    await get_pool().on_error(
                        token.id, 429,
                        retry_after_seconds=_GEMINI_429_COOLDOWN,
                    )
                elif status:
                    await get_pool().on_error(token.id, status)
                raise

    return PoolGeminiReranker(
        config=LLMConfig(api_key="placeholder", model=model),
    )
