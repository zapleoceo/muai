"""L2 — Pattern node: mining, maintenance, candidate lookup.

A Pattern node represents a recurring (context, action) pair learned from
Dima's decisions. Counts drive the weight used by decide.scoring:

  weight = confirmation_count − 2 × correction_count + 0.25 × observation_count

Two stable keys per pattern:
  context_key  — hash of entity hints only (person, chat, folder …)
                 used to FIND candidate patterns for a given event
  signature    — hash of (context_key + action_label)
                 used to identify a specific (context, action) pair
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------

def context_key_for(event_hints: list[dict] | None) -> str:
    """Stable key for the event context (no action label).
    Used to look up all candidate actions for a given event shape."""
    parts: list[str] = []
    for h in event_hints or []:
        if h.get("type") in ("person", "account", "chat", "folder", "topic"):
            parts.append(f"{h.get('type')}:{h.get('identifier', '')}")
    parts.sort()
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:24]


def signature_for(event_hints: list[dict] | None, action_label: str) -> str:
    """Stable key for a (context, action) pair."""
    ctx = context_key_for(event_hints)
    action_part = f"action:{(action_label or '').strip().lower()}"
    return hashlib.sha1(f"{ctx}|{action_part}".encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Public writes (called from decide-time and feedback loop)
# ---------------------------------------------------------------------------

async def upsert_observation(signature: str, context_key: str,
                              action_label: str, tool: str | None,
                              args: dict | None) -> None:
    await _bump(signature, context_key, action_label, tool, args,
                field="observation_count")


async def upsert_confirmation(signature: str, context_key: str,
                               action_label: str, tool: str | None,
                               args: dict | None) -> None:
    await _bump(signature, context_key, action_label, tool, args,
                field="confirmation_count")


async def upsert_correction(signature: str, context_key: str,
                             action_label: str, tool: str | None,
                             args: dict | None) -> None:
    await _bump(signature, context_key, action_label, tool, args,
                field="correction_count")


# ---------------------------------------------------------------------------
# Public reads (called from decide.scoring)
# ---------------------------------------------------------------------------

async def get_pattern(signature: str) -> dict | None:
    """Return pattern counts + weight for a specific (context, action) sig."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        r = await ses.run("MATCH (p:Pattern {id: $id}) RETURN p", id=signature)
        row = await r.single()
        if row is None:
            return None
        node = row["p"]
        return _node_to_dict(node)


async def get_candidates(context_key: str, limit: int = 5) -> list[dict]:
    """Return confirmed patterns for a given context key, most-confirmed first.
    Falls back to globally-confirmed patterns when context has no matches."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (p:Pattern) "
            "WHERE p.context_key = $ctx AND p.action_label IS NOT NULL "
            "  AND p.confirmation_count > 0 "
            "RETURN p ORDER BY p.confirmation_count DESC LIMIT $lim",
            ctx=context_key, lim=limit,
        )
        rows = [rec async for rec in r]
        if not rows:
            r = await ses.run(
                "MATCH (p:Pattern) "
                "WHERE p.action_label IS NOT NULL AND p.confirmation_count > 0 "
                "RETURN p ORDER BY p.confirmation_count DESC LIMIT $lim",
                lim=limit,
            )
            rows = [rec async for rec in r]
    return [_node_to_dict(rec["p"]) for rec in rows]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _node_to_dict(node: Any) -> dict:
    raw_args = node.get("args_template")
    try:
        parsed_args = json.loads(raw_args) if raw_args else None
    except (TypeError, ValueError):
        parsed_args = None
    return {
        "signature":          node.get("id"),
        "context_key":        node.get("context_key"),
        "action_label":       node.get("action_label"),
        "tool":               node.get("tool"),
        "args":               parsed_args,
        "observation_count":  node.get("observation_count", 0),
        "confirmation_count": node.get("confirmation_count", 0),
        "correction_count":   node.get("correction_count", 0),
        "weight":             _weight(node),
        "last_seen_at":       node.get("last_seen_at"),
    }


def _weight(node: Any) -> float:
    obs  = float(node.get("observation_count", 0) or 0)
    conf = float(node.get("confirmation_count", 0) or 0)
    corr = float(node.get("correction_count", 0) or 0)
    return conf - 2.0 * corr + 0.25 * obs


async def _bump(signature: str, context_key: str, action_label: str,
                tool: str | None, args: dict | None, *, field: str) -> None:
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    now = datetime.utcnow().isoformat()
    args_json = json.dumps(args, ensure_ascii=False) if args else None
    async with client.driver.session(database=db) as ses:
        await ses.run(
            f"MERGE (p:Pattern {{id: $id}}) "
            f"ON CREATE SET "
            f"  p.action_label=$label, p.tool=$tool, "
            f"  p.args_template=$args, p.context_key=$ctx, "
            f"  p.observation_count=0, p.confirmation_count=0, "
            f"  p.correction_count=0, p.created_at=$now "
            f"SET p.{field}   = coalesce(p.{field}, 0) + 1, "
            f"    p.context_key = $ctx, "
            f"    p.last_seen_at = $now",
            id=signature, label=action_label, tool=tool,
            args=args_json, ctx=context_key, now=now,
        )
    log.debug("pattern %s field=%s bumped", signature[:8], field)
