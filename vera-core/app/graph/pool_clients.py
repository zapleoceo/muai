"""Graphiti LLM/Embedder/Reranker clients that rotate Gemini keys via our pool."""

import logging

from vera_shared.tokens.pool import TokensExhausted, get_pool
from vera_shared.tokens.selector import get_token

log = logging.getLogger(__name__)


def _status_from_exc(exc: Exception) -> int:
    # Graphiti normalises 429 → RateLimitError; detect by class first so
    # we don't depend on substring matching.
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
    """Pick a fresh Gemini key from the pool and rebuild the underlying google client."""
    from google import genai

    token = await get_token("gemini", capability)
    holder.client = genai.Client(api_key=token.token)
    return token


# -------- LLM ---------------------------------------------------------------


async def _build_deepseek_fallback() -> object | None:
    """Lazy DeepSeek-via-OpenAI Graphiti client; rebuilt per call so we
    pick a fresh deepseek key from the pool each time."""
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from openai import AsyncOpenAI
    try:
        token = await get_token("deepseek", "chat:fast")
    except TokensExhausted:
        return None
    aclient = AsyncOpenAI(api_key=token.token,
                          base_url="https://api.deepseek.com")
    client = OpenAIGenericClient(
        config=LLMConfig(api_key=token.token, model="deepseek-chat"),
        client=aclient,
    )
    return client


def make_llm_client(model: str = "gemini-2.5-flash-lite", capability: str = "chat:fast"):
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

    class PoolGeminiClient(GeminiClient):
        async def _generate_response(self, *args, **kwargs):
            try:
                token = await _refresh_gemini_client(self, capability=capability)
            except TokensExhausted:
                fallback = await _build_deepseek_fallback()
                if fallback is None:
                    raise
                log.warning("Gemini exhausted — falling back to DeepSeek for this call")
                return await fallback._generate_response(*args, **kwargs)
            try:
                return await super()._generate_response(*args, **kwargs)
            except TokensExhausted:
                fallback = await _build_deepseek_fallback()
                if fallback is None:
                    raise
                log.warning("Gemini exhausted mid-call — falling back to DeepSeek")
                return await fallback._generate_response(*args, **kwargs)
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(token.id, status)
                # Free-tier Gemini → 429 / 503 / quota: fall back to
                # DeepSeek for THIS call rather than re-raising.
                if status in (429, 503):
                    fallback = await _build_deepseek_fallback()
                    if fallback is not None:
                        log.warning("Gemini %s — falling back to DeepSeek", status)
                        return await fallback._generate_response(*args, **kwargs)
                raise

    # placeholder key satisfies parent __init__; rebuilt per call
    return PoolGeminiClient(config=LLMConfig(api_key="placeholder", model=model))


# -------- Embedder ----------------------------------------------------------


def make_embedder(embedding_model: str = "gemini-embedding-001"):
    from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig

    class PoolGeminiEmbedder(GeminiEmbedder):
        async def create(self, input_data):
            token = await _refresh_gemini_client(self, capability="chat:fast")
            try:
                return await super().create(input_data)
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(token.id, status)
                raise

        async def create_batch(self, input_data_list):
            token = await _refresh_gemini_client(self, capability="chat:fast")
            try:
                return await super().create_batch(input_data_list)
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(token.id, status)
                raise

    return PoolGeminiEmbedder(
        config=GeminiEmbedderConfig(api_key="placeholder", embedding_model=embedding_model),
    )


# -------- Reranker ----------------------------------------------------------


def make_reranker(model: str = "gemini-2.5-flash-lite"):
    from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
    from graphiti_core.llm_client.config import LLMConfig

    class PoolGeminiReranker(GeminiRerankerClient):
        async def rank(self, query, passages):
            token = await _refresh_gemini_client(self, capability="chat:fast")
            try:
                return await super().rank(query, passages)
            except Exception as exc:
                status = _status_from_exc(exc)
                if status:
                    await get_pool().on_error(token.id, status)
                raise

    return PoolGeminiReranker(
        config=LLMConfig(api_key="placeholder", model=model),
    )
