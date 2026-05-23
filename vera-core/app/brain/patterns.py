"""L2 — Pattern node mining and maintenance.

A Pattern node represents a recurring (trigger_signature, action) pair
discovered from past decisions. Each Pattern has:
  - signature: stable hash of the event shape that triggered it
    (source, sender_key, intent_class, ...)
  - action: the tool + args template chosen
  - observation_count: how many times Vera saw this signature
  - confirmation_count: how many times Dima confirmed the action
  - correction_count: how many times Dima corrected away from it
  - weight: derived score (confirmations − 2×corrections + 1×observations)

The brain reads patterns at decide-time; alignment scoring uses a
matching pattern's weight as one of the components.

This file owns the writes; decide/scoring.py owns the reads.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)


def signature_for(event_hints: list[dict] | None, action_label: str) -> str:
    """Deterministic key for a (trigger, action) pair. We use only the
    most stable hints (sender + chat/folder) so noise in the body
    doesn't fragment patterns."""
    parts: list[str] = []
    for h in event_hints or []:
        if h.get("type") in ("person", "account", "chat", "folder", "topic"):
            parts.append(f"{h.get('type')}:{h.get('identifier','')}")
    parts.sort()
    parts.append(f"action:{(action_label or '').strip().lower()}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:24]


async def upsert_observation(signature: str, action_label: str,
                              tool: str | None, args: dict | None) -> None:
    """Increment observation_count for this signature; create if new."""
    await _bump(signature, action_label, tool, args, field="observation_count")


async def upsert_confirmation(signature: str, action_label: str,
                               tool: str | None, args: dict | None) -> None:
    await _bump(signature, action_label, tool, args, field="confirmation_count")


async def upsert_correction(signature: str, action_label: str,
                             tool: str | None, args: dict | None) -> None:
    await _bump(signature, action_label, tool, args, field="correction_count")


async def get_pattern(signature: str) -> dict | None:
    """Read a pattern's counts back. Returns None if it doesn't exist."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (p:Pattern {id: $id}) RETURN p", id=signature,
        )
        row = await r.single()
        if row is None:
            return None
        node = row["p"]
        return {
            "signature": node.get("id"),
            "action_label": node.get("action_label"),
            "tool": node.get("tool"),
            "observation_count": node.get("observation_count", 0),
            "confirmation_count": node.get("confirmation_count", 0),
            "correction_count": node.get("correction_count", 0),
            "weight": _weight(node),
            "last_seen_at": node.get("last_seen_at"),
        }


def _weight(node: Any) -> float:
    obs = float(node.get("observation_count", 0) or 0)
    conf = float(node.get("confirmation_count", 0) or 0)
    corr = float(node.get("correction_count", 0) or 0)
    # Simple: confirmations are strong positive signal, corrections strong
    # negative. Raw observations carry small weight (the event was seen
    # but Dima never weighed in either way).
    return conf - 2.0 * corr + 0.25 * obs


async def _bump(signature: str, action_label: str, tool: str | None,
                args: dict | None, *, field: str) -> None:
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    now = datetime.utcnow().isoformat()
    async with client.driver.session(database=db) as ses:
        await ses.run(
            f"MERGE (p:Pattern {{id: $id}}) "
            f"ON CREATE SET p.action_label=$label, p.tool=$tool, "
            f"  p.args_template=$args, p.observation_count=0, "
            f"  p.confirmation_count=0, p.correction_count=0, "
            f"  p.created_at=$now "
            f"SET p.{field} = coalesce(p.{field}, 0) + 1, "
            f"    p.last_seen_at=$now",
            id=signature, label=action_label, tool=tool,
            args=str(args) if args else None, now=now,
        )
        log.debug("pattern %s field=%s bumped", signature, field)
