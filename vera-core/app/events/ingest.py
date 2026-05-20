import asyncio
import json
import logging
from datetime import datetime

from app.config import get_settings
from app.events.store import mark_episode

log = logging.getLogger(__name__)


def _format_episode_body(
    source: str, category: str, content_text: str | None,
    entity_hints: list | None, metadata: dict | None,
) -> str:
    parts: list[str] = []
    if content_text:
        parts.append(content_text)
    if entity_hints:
        parts.append("Entities involved:")
        for h in entity_hints:
            t = h.get("type", "?")
            ident = h.get("identifier") or h.get("name") or "?"
            extra = ", ".join(f"{k}={v}" for k, v in h.items()
                              if k not in ("type", "identifier", "name"))
            line = f"- {t}: {ident}"
            if extra:
                line += f" ({extra})"
            parts.append(line)
    if metadata:
        parts.append("Context: " + json.dumps(metadata, ensure_ascii=False, default=str))
    return "\n".join(parts) or f"(empty {source} event)"


async def ingest_episode(event_id: int, *, source: str, category: str,
                          content_text: str | None, entity_hints: list | None,
                          metadata: dict | None, occurred_at: datetime) -> None:
    """Add episode to Graphiti in the background. Errors are logged, not raised."""
    try:
        from graphiti_core.nodes import EpisodeType

        from app.graph.client import get_graphiti

        client = await get_graphiti()
        body = _format_episode_body(source, category, content_text, entity_hints, metadata)

        # Graphiti will create an episodic node + extract entities/relationships
        await client.add_episode(
            name=f"{source}/{event_id}",
            episode_body=body,
            source=EpisodeType.text,
            source_description=f"{source} event ({category})",
            reference_time=occurred_at,
            group_id="vera",
        )
        # Graphiti doesn't return the episode uuid directly; we mark "done"
        # for now by storing the name. Future: query for the node uuid.
        await mark_episode(event_id, f"{source}/{event_id}")
        log.info("Episode ingested: %s/%d", source, event_id)
    except Exception as exc:
        log.exception("Episode ingest failed for event %d: %s", event_id, exc)

    # Run triage regardless of episode ingestion outcome — triage uses
    # whatever Graphiti can return (may be empty for first events).
    try:
        from app.triage.dispatcher import schedule_triage
        schedule_triage(event_id)
    except Exception as exc:
        log.warning("Triage schedule failed for event %d: %s", event_id, exc)


def schedule_ingest(event_id: int, **kw) -> None:
    """Fire-and-forget background ingestion (returns immediately)."""
    asyncio.create_task(ingest_episode(event_id, **kw))
