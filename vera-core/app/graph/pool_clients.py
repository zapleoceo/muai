"""Graphiti LLM/Embedder/Reranker clients that rotate Gemini keys via our pool.

Key rotation strategy for Graphiti:
  - On 429: cooldown the key for 1 hour, rotate to the next available Gemini key.
  - On 503: cooldown 5 min, rotate.
  - On 401/403: cooldown 1 hour, rotate.
  - On TokensExhausted: all Gemini keys unavailable — raise so caller skips the write.
  - No DeepSeek fallback: DeepSeek rejects response_format:json_schema used by Graphiti.
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


async def _refresh_gemini_client(holder, *, capability: str = "chat:fast"):
    from google import genai
    token = await get_token("gemini", capability)
    holder.client = genai.Client(api_key=token.token)
    return token


# ── LLM ──────────────────────────────────────────────────────────────────────


def make_llm_client(model: str = "gemini-3.5-flash", capability: str = "chat:fast"):
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

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

    class PoolVoyageEmbedder(VoyageAIEmbedder):
        async def create(self, input_data):
            tok = await _swap_voyage_client(self)
            try:
                return await super().create(input_data)
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(tok.id, status)
                raise

        async def create_batch(self, input_data_list):
            tok = await _swap_voyage_client(self)
            try:
                return await super().create_batch(input_data_list)
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


def make_reranker(model: str = "gemini-3.5-flash"):
    from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
    from graphiti_core.llm_client.config import LLMConfig

    class PoolGeminiReranker(GeminiRerankerClient):
        async def rank(self, query, passages):
            token = await _refresh_gemini_client(self, capability="chat:fast")
            try:
                return await super().rank(query, passages)
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
