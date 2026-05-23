"""Event → graph ingest.

Two paths:
  - ingest(envelope): synchronous. Dedup, save Event row, write cheap
    deterministic edges to graph from entity_hints. Enqueues IngestJob
    for deferred LLM extraction. Idempotent (dedup by source_event_id).
  - deep_extract(event_id): async, run by jobs runner. Heavy LLM-based
    entity + relation extraction via Graphiti. ~30s budget per event.

Cheap edges (no LLM):
  Event —[FROM]→ Person|Account
  Event —[IN]→ Folder|Topic|Chat
  Event —[MENTIONS]→ entity_hint matched against existing graph
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from app.events.store import save_event
from app.sources.base import EventEnvelope
from vera_shared.db.engine import get_session
from vera_shared.db.models import IngestJob

log = logging.getLogger(__name__)


async def ingest(env: EventEnvelope) -> int | None:
    """Persist envelope as an Event, write cheap edges, enqueue deep
    extraction. Returns event_id, or None if duplicate."""
    event, is_new = await save_event(
        source=env.source, source_event_id=env.source_event_id,
        account=env.account, category="generic",
        content_text=env.content_text,
        content_extra=None,
        entity_hints=[asdict(h) for h in env.entity_hints] or None,
        metadata=env.metadata or None,
        occurred_at=env.occurred_at,
    )
    if not is_new:
        return None

    try:
        await _write_cheap_edges(event.id, env)
    except Exception as exc:
        log.warning("cheap edges failed for event=%s: %s", event.id, exc)

    await _enqueue_deep(event.id)
    return event.id


async def deep_extract(event_id: int) -> None:
    """Run heavy LLM extraction. Pulls event, runs Graphiti add_episode,
    persists episode_uuid back on the Event row."""
    from app.events.store import mark_episode
    from app.graph.client import get_graphiti
    from sqlalchemy import select
    from vera_shared.db.models import Event

    async with get_session() as s:
        ev = (await s.execute(
            select(Event).where(Event.id == event_id)
        )).scalar_one_or_none()
    if ev is None:
        log.warning("deep_extract: event %s not found", event_id)
        return

    client = await get_graphiti()
    name = f"{ev.source}:{ev.source_event_id or ev.id}"
    body = ev.content_text or ""
    if not body.strip():
        return
    try:
        episode = await client.add_episode(
            name=name, episode_body=body, source_description=ev.source,
            reference_time=ev.occurred_at,
        )
        uuid = getattr(episode, "uuid", None) or getattr(
            getattr(episode, "episode", None), "uuid", None)
        await mark_episode(event_id, uuid)
    except Exception as exc:
        log.exception("Graphiti add_episode failed for event=%s: %s",
                      event_id, exc)
        raise


async def _enqueue_deep(event_id: int) -> None:
    async with get_session() as s:
        s.add(IngestJob(event_id=event_id))
        await s.commit()


async def _write_cheap_edges(event_id: int, env: EventEnvelope) -> None:
    """Deterministic graph writes — no LLM. Idempotent via MERGE.

    Schema (informal — Graphiti owns formal schema):
      (:Event {id: 'src:sid'}) —[:FROM]→ (:Person {id: 'email|@handle'})
      (:Event)                 —[:IN]→ (:Container {id: 'folder|topic|chat'})
      (:Event)                 —[:MENTIONS]→ (:Entity {id: ...})
    """
    if not env.entity_hints:
        return
    from app.graph.client import get_graphiti
    from app.config import get_settings

    client = await get_graphiti()
    db = get_settings().neo4j_database
    event_key = f"{env.source}:{env.source_event_id}"

    async with client.driver.session(database=db) as ses:
        await ses.run(
            "MERGE (e:Event {id: $id}) "
            "SET e.source=$source, e.occurred_at=$ts, e.account=$account",
            id=event_key, source=env.source,
            ts=env.occurred_at.isoformat(), account=env.account,
        )
        for h in env.entity_hints:
            label = _entity_label(h.type)
            rel = _relation_for(h.type)
            await ses.run(
                f"MATCH (e:Event {{id: $eid}}) "
                f"MERGE (n:{label} {{id: $nid}}) "
                f"SET n.name = coalesce(n.name, $name), n.type = $type "
                f"MERGE (e)-[:{rel}]->(n)",
                eid=event_key, nid=h.identifier,
                name=h.name, type=h.type,
            )


def _entity_label(hint_type: str) -> str:
    return {
        "person": "Person",
        "account": "Account",
        "folder": "Container",
        "topic": "Container",
        "chat": "Container",
        "project": "Project",
        "domain": "Domain",
    }.get(hint_type, "Entity")


def _relation_for(hint_type: str) -> str:
    return {
        "person": "FROM",
        "account": "FROM",
        "folder": "IN",
        "topic": "IN",
        "chat": "IN",
    }.get(hint_type, "MENTIONS")
