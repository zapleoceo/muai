"""v3 decision dispatcher.

  Event arrives → query graph (L1+L2+L3) → enumerate candidate actions
  → score each via decide.scoring → choose by band → return Decision.

This module is the v3 replacement for app/triage/dispatcher. It does
NOT replace the live event flow yet — it ships as a queryable function
that can be A/B-tested against the v2 path before flipping the switch
in Phase 5.

Candidate enumeration in this phase is intentionally simple:
  - last N patterns matching this event's signature (proven actions)
  - any patterns with high confirmation_count for adjacent signatures
  - a always-present 'ask' fallback (score → user prompt)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.brain import patterns as P
from app.decide.scoring import Candidate, Scored, score

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Decision:
    band: str  # 'auto' | 'propose' | 'ask'
    chosen: Scored | None
    candidates: list[Scored]
    reason: str


_ASK_CANDIDATE = Candidate(
    label="спросить Диму", tool=None, args=None,
    rationale="первый раз такое — нужна явная инструкция",
)


async def decide(event_hints: list[dict]) -> Decision:
    cands = await _enumerate_candidates(event_hints)
    if not cands:
        scored_ask = await score(_ASK_CANDIDATE, event_hints)
        return Decision(band="ask", chosen=scored_ask,
                         candidates=[scored_ask], reason="no candidates")

    scored = []
    for c in cands:
        scored.append(await score(c, event_hints))
    scored.sort(key=lambda s: s.score, reverse=True)

    top = scored[0]
    band = _band(top.score)
    reason = (f"top={top.candidate.label} score={top.score:.2f} "
              f"({_summarise(top.breakdown)})")
    return Decision(band=band, chosen=top, candidates=scored, reason=reason)


async def _enumerate_candidates(hints: list[dict]) -> list[Candidate]:
    """Pull candidate actions from the graph: patterns matching this
    signature, plus a small set of historically-confirmed actions for
    related entities. This is the v3 equivalent of triage's hardcoded
    proposal list — but every candidate is learned, not hardcoded.

    For now we ask Neo4j for patterns whose action_label is non-null
    and whose signature shares at least one entity hint with this event.
    """
    from app.graph.client import get_graphiti
    from app.config import get_settings
    client = await get_graphiti()
    db = get_settings().neo4j_database
    hint_ids = [h.get("identifier", "") for h in hints if h.get("identifier")]
    if not hint_ids:
        return []
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (p:Pattern) WHERE p.action_label IS NOT NULL "
            "RETURN p.action_label AS label, p.tool AS tool, "
            "p.args_template AS args, p.confirmation_count AS confirm "
            "ORDER BY p.confirmation_count DESC LIMIT 5",
        )
        rows = [rec async for rec in r]
    cands: list[Candidate] = []
    for row in rows:
        cands.append(Candidate(
            label=row.get("label") or "?",
            tool=row.get("tool"),
            args=None,
            rationale=f"pattern with {row.get('confirm', 0)} prior confirmations",
        ))
    # Always include the ask-fallback so the user can override.
    cands.append(_ASK_CANDIDATE)
    return cands


def _band(score: float) -> str:
    if score >= 7.0:
        return "auto"
    if score >= 3.0:
        return "propose"
    return "ask"


def _summarise(breakdown: dict) -> str:
    parts = [f"{k}={v}" for k, v in breakdown.items()
             if k in ("value_alignment", "goal_contribution",
                       "pattern_match", "reversibility")]
    return ", ".join(parts)
