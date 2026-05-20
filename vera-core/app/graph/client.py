import asyncio
import logging

from app.config import get_settings
from vera_shared.tokens import repository as token_repo

log = logging.getLogger(__name__)

_client = None
_lock = asyncio.Lock()
_indices_built = False


async def _pick_token(provider: str) -> str:
    for t in await token_repo.get_all_active():
        if t.provider == provider:
            return t.token
    raise RuntimeError(f"No active {provider} token in DB")


async def get_graphiti():
    global _client, _indices_built
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client

        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
        from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig
        from graphiti_core.llm_client.gemini_client import GeminiClient
        from graphiti_core.llm_client.config import LLMConfig

        settings = get_settings()
        if not settings.neo4j_uri:
            raise RuntimeError("NEO4J_URI not configured")

        gemini_key = await _pick_token("gemini")
        voyage_key = await _pick_token("voyage")

        llm_client = GeminiClient(
            config=LLMConfig(api_key=gemini_key, model="gemini-2.5-flash"),
        )
        embedder = VoyageAIEmbedder(
            config=VoyageAIEmbedderConfig(
                api_key=voyage_key, embedding_model="voyage-3",
            ),
        )

        reranker = GeminiRerankerClient(
            config=LLMConfig(api_key=gemini_key, model="gemini-2.5-flash-lite"),
        )

        client = Graphiti(
            settings.neo4j_uri,
            settings.neo4j_username,
            settings.neo4j_password,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=reranker,
        )
        # Aura Free uses the instance id as default database name.
        try:
            client.database = settings.neo4j_database
        except Exception:
            pass
        try:
            client.driver._default_workspace_config.database = settings.neo4j_database
        except Exception:
            pass
        _client = client
        log.info("Graphiti client initialised (Neo4j: %s)", settings.neo4j_uri)
    return _client


async def ensure_indices() -> None:
    global _indices_built
    if _indices_built:
        return
    client = await get_graphiti()
    await client.build_indices_and_constraints()
    _indices_built = True
    log.info("Graphiti indices/constraints verified")
