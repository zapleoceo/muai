import asyncio
import json
import logging
from datetime import datetime

from app.events.store import mark_episode

log = logging.getLogger(__name__)

# Concurrency cap on parallel add_episode calls. Empirically 3 keeps Gemini
# embed quota happy while draining backlog fast enough that pollers don't
# starve the queue.
_INGEST_SEM = asyncio.Semaphore(3)
_INGEST_TIMEOUT = 45.0
_MAX_RETRIES = 2


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


async def _add_with_retry(client, **kwargs) -> bool:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with _INGEST_SEM:
                await asyncio.wait_for(client.add_episode(**kwargs),
                                       timeout=_INGEST_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            log.warning("Episode add timed out (attempt %d) for %s",
                        attempt + 1, kwargs.get("name"))
        except Exception as exc:
            msg = str(exc).lower()
            transient = any(x in msg for x in ("rate", "429", "timeout", "503", "504"))
            if attempt < _MAX_RETRIES and transient:
                wait = 2 ** attempt
                log.info("Transient ingest failure, retrying in %ds: %s", wait, exc)
                await asyncio.sleep(wait)
                continue
            log.warning("Episode add failed (permanent): %s", exc)
            return False
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(2 ** attempt)
    return False


async def ingest_episode(event_id: int, *, source: str, category: str,
                          content_text: str | None, entity_hints: list | None,
                          metadata: dict | None, occurred_at: datetime) -> None:
    """Add episode to Graphiti. Errors are logged, not raised."""
    try:
        from graphiti_core.nodes import EpisodeType

        from app.graph.client import get_graphiti

        client = await get_graphiti()
        body = _format_episode_body(source, category, content_text, entity_hints, metadata)
        ok = await _add_with_retry(
            client,
            name=f"{source}/{event_id}",
            episode_body=body,
            source=EpisodeType.text,
            source_description=f"{source} event ({category})",
            reference_time=occurred_at,
            group_id="vera",
        )
        if ok:
            await mark_episode(event_id, f"{source}/{event_id}")
            log.info("Episode ingested: %s/%d", source, event_id)
    except Exception as exc:
        log.exception("Episode ingest failed for event %d: %s", event_id, exc)


def schedule_ingest(event_id: int, **kw) -> None:
    """Fire-and-forget ingest + triage in PARALLEL.
    Triage no longer waits for graph write — it reads whatever is already
    there. The graph fills in over time as ingests complete."""
    from app.common.bg import spawn
    spawn(ingest_episode(event_id, **kw), name=f"ingest-{event_id}")
    try:
        from app.triage.dispatcher import schedule_triage
        schedule_triage(event_id)
    except Exception as exc:
        log.warning("Triage schedule failed for event %d: %s", event_id, exc)
