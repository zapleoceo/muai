"""One-shot: wipe v2 data so we can boot v3 with a clean brain.

KEEPS:
  - tokens (LLM provider keys, encrypted)
  - gmail_accounts (OAuth refresh tokens — losing these = re-do OAuth)
  - sources (Telegram filter config)
  - mcp_servers (runtime registration of MCP children)
  - agents (HTTP agent registry, refilled on next heartbeat)
  - settings.user_prefs.forum_chat_id + use_topics (one-shot UX)

WIPES:
  - events table (~500 rows from v2 polling)
  - triggers, decision_replay, mcp_proposals, pending_followups,
    pending_instructions (whole tables truncated)
  - settings.user_prefs.* except forum_chat_id + use_topics
  - settings.persona (sparse v2 digest)
  - all Neo4j nodes and relationships
  - all forum topics in the «Вера бот» supergroup (they're attached
    to old event ids)

After this runs: Vera is amnesiac on inputs but knows where to plug in.
Phase 0 backfill will repopulate the graph from scratch.
"""
import asyncio
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("wipe")


async def wipe_sqlite() -> None:
    from sqlalchemy import delete, text
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import (
        DecisionReplay, Event, MCPProposal, PendingFollowup, Setting,
        Trigger,
    )

    async with get_session() as s:
        for model in (Event, DecisionReplay, MCPProposal,
                      PendingFollowup, Trigger):
            r = await s.execute(delete(model))
            log.info("  SQL wiped %s rows from %s",
                     r.rowcount, model.__tablename__)

        # Strip behaviour prefs, keep only UX placement
        row = await s.get(Setting, "user_prefs")
        if row and isinstance(row.value, dict):
            keep = {k: row.value[k] for k in ("forum_chat_id", "use_topics")
                    if k in row.value}
            row.value = keep
            log.info("  SQL user_prefs reduced to %s", list(keep))
        # Drop persona digest — will rebuild from graph
        persona = await s.get(Setting, "persona")
        if persona:
            await s.delete(persona)
            log.info("  SQL settings.persona deleted")

        await s.commit()


async def wipe_neo4j() -> None:
    from app.graph.client import get_graphiti
    from app.config import get_settings

    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        for label in ("Episodic", "Entity", "Community"):
            r = await ses.run(f"MATCH (n:{label}) DETACH DELETE n RETURN count(n)")
            row = await r.single()
            log.info("  Neo4j deleted %s :%s nodes", row[0], label)
        # Sweep any remaining
        r = await ses.run("MATCH (n) DETACH DELETE n RETURN count(n)")
        log.info("  Neo4j sweep: removed %s remaining nodes",
                 (await r.single())[0])


async def wipe_topics() -> None:
    """Delete all forum topics in the configured forum_chat_id."""
    import httpx
    token = os.environ.get("TELEGRAM_BOT_TOKEN_VERA")
    if not token:
        log.warning("  TELEGRAM_BOT_TOKEN_VERA not in env — skipping topic wipe")
        return
    from sqlalchemy import select
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import Setting

    async with get_session() as s:
        row = await s.get(Setting, "user_prefs")
        forum_chat = int((row.value or {}).get("forum_chat_id") or 0) if row else 0
    if not forum_chat:
        log.info("  no forum_chat_id set — skipping topic wipe")
        return

    api = f"https://api.telegram.org/bot{token}"
    deleted = 0
    async with httpx.AsyncClient(timeout=20) as c:
        for thread_id in range(2, 500):  # sweep range; nonexistent → 400 (ok)
            r = await c.post(f"{api}/deleteForumTopic",
                             json={"chat_id": forum_chat,
                                   "message_thread_id": thread_id})
            if r.status_code == 200 and r.json().get("ok"):
                deleted += 1
    log.info("  Telegram: deleted %s forum topics in %s", deleted, forum_chat)


async def main() -> None:
    log.info("=== Vera v3 wipe ===")
    log.info("[1/3] SQLite tables")
    await wipe_sqlite()
    log.info("[2/3] Neo4j graph")
    await wipe_neo4j()
    log.info("[3/3] Forum topics")
    await wipe_topics()
    log.info("=== done — restart vera-core for fresh ingest queue ===")


if __name__ == "__main__":
    asyncio.run(main())
