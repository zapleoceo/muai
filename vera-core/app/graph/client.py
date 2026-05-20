import asyncio
import logging

from app.config import get_settings

log = logging.getLogger(__name__)

_client = None
_lock = asyncio.Lock()
_indices_built = False


async def get_graphiti():
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client

        from graphiti_core import Graphiti
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        from app.graph.pool_clients import make_embedder, make_llm_client, make_reranker

        settings = get_settings()
        if not settings.neo4j_uri:
            raise RuntimeError("NEO4J_URI not configured")

        driver = Neo4jDriver(
            settings.neo4j_uri,
            settings.neo4j_username,
            settings.neo4j_password,
            database=settings.neo4j_database,
        )
        _client = Graphiti(
            graph_driver=driver,
            llm_client=make_llm_client(),
            embedder=make_embedder(),
            cross_encoder=make_reranker(),
        )
        log.info("Graphiti client initialised (Neo4j: %s, db=%s) with pooled Gemini keys",
                 settings.neo4j_uri, settings.neo4j_database)
    return _client


async def ensure_indices() -> None:
    global _indices_built
    if _indices_built:
        return
    client = await get_graphiti()
    await client.build_indices_and_constraints()
    _indices_built = True
    log.info("Graphiti indices/constraints verified")
