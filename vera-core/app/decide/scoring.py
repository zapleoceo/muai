"""Alignment scoring — confidence as graph-alignment, no manual thresholds.

For each candidate action Vera scores it on 5 components, each 0..1,
then combines into a 0..10 alignment score:

  value_alignment   — does this action match active Value nodes for Dima?
  goal_contribution — does it advance an active Goal node?
  pattern_match     — has Dima confirmed this signature before? (L2)
  reversibility     — easy to undo? (from tool_router.tool_reversibility)
  novelty_penalty   — first time we see this combination? (sub from total)

Hard rules:
  - Any NoGo node match → score = 0 (action is killed)
  - Tool not in AUTO_SAFE_TOOLS → cap at 5.9 (must propose, not auto)

Output bands (set in decide.dispatch, not here):
  ≥ 7    → auto-execute
  3 – 6  → propose with reasoning + buttons
  < 3    → ask plain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.brain import patterns as P
from app.config import get_settings
from app.orchestrator.tool_router import AUTO_SAFE_TOOLS, tool_reversibility

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Candidate:
    label: str
    tool: str | None
    args: dict | None
    rationale: str = ""


@dataclass(slots=True)
class Scored:
    candidate: Candidate
    score: float
    breakdown: dict
    blocked_by: str | None = None  # NoGo node id if killed


async def score(candidate: Candidate, event_hints: list[dict]) -> Scored:
    """Compute alignment score against current graph state."""
    nogo = await _nogo_violations(candidate, event_hints)
    if nogo:
        return Scored(candidate=candidate, score=0.0,
                      breakdown={"nogo_violation": nogo[0]},
                      blocked_by=nogo[0])

    hints = event_hints
    ctx   = P.context_key_for(hints)
    sig   = P.signature_for(hints, candidate.label)
    pattern = await P.get_pattern(sig)

    pattern_match     = _pattern_component(pattern)
    value_alignment   = await _value_alignment(candidate, hints)
    goal_contribution = await _goal_contribution(candidate, hints)
    reversibility     = tool_reversibility(candidate.tool)

    obs = (pattern or {}).get("observation_count", 0)
    novelty_penalty = 0.0 if obs >= 3 else (0.5 if obs == 0 else 0.2)

    raw = (
        2.5 * value_alignment
        + 2.5 * goal_contribution
        + 3.0 * pattern_match
        + 2.0 * reversibility
        - 1.5 * novelty_penalty
    )
    final = max(0.0, min(10.0, raw))

    if candidate.tool and candidate.tool not in AUTO_SAFE_TOOLS and final >= 7.0:
        final = 6.9  # cap: needs explicit auto_safe status to fire unattended

    return Scored(
        candidate=candidate, score=final,
        breakdown={
            "value_alignment":   round(value_alignment, 3),
            "goal_contribution": round(goal_contribution, 3),
            "pattern_match":     round(pattern_match, 3),
            "reversibility":     round(reversibility, 3),
            "novelty_penalty":   round(novelty_penalty, 3),
            "pattern_obs":       obs,
            "pattern_confirm":   (pattern or {}).get("confirmation_count", 0),
            "pattern_correct":   (pattern or {}).get("correction_count", 0),
        },
    )


def _pattern_component(pattern: dict | None) -> float:
    if pattern is None:
        return 0.0
    w = float(pattern.get("weight", 0.0) or 0.0)
    if w <= 0:
        return 0.0
    return min(1.0, w / 10.0)


async def _nogo_violations(c: Candidate, hints: list[dict]) -> list[str]:
    """Match candidate against (:NoGo) nodes in the graph."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    targets = [h.get("identifier", "") for h in hints if h.get("identifier")]
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (n:NoGo) RETURN n.id AS id, n.tool_pattern AS tp, "
            "n.targets AS targets",
        )
        rows = [rec async for rec in r]
    out: list[str] = []
    for row in rows:
        tp   = row.get("tp") or ""
        tgts = row.get("targets") or []
        tool_match   = tp and c.tool and tp.lower() in c.tool.lower()
        target_match = tgts and any(t in targets for t in tgts)
        if tool_match and (not tgts or target_match):
            out.append(row.get("id"))
    return out


async def _value_alignment(c: Candidate, hints: list[dict]) -> float:
    """Neutral 0.5 until Phase 2 populates (:Value) nodes."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    if not c.tool:
        return 0.5
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (v:Value) WHERE v.tool_pattern IS NULL "
            "OR toLower(v.tool_pattern) CONTAINS toLower($tool) "
            "RETURN sum(coalesce(v.weight, 1.0)) AS w",
            tool=c.tool,
        )
        row = await r.single()
        w = float(row.get("w") or 0) if row else 0
    if w <= 0:
        return 0.5
    return min(1.0, 0.5 + w / 10.0)


async def _goal_contribution(c: Candidate, hints: list[dict]) -> float:
    """Neutral 0.5 until Phase 2 populates (:Goal) nodes."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    hint_ids = [h.get("identifier", "") for h in hints if h.get("identifier")]
    if not hint_ids:
        return 0.5
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            "MATCH (g:Goal {status: 'active'})-[:ABOUT]->(e) "
            "WHERE e.id IN $ids RETURN count(g) AS n",
            ids=hint_ids,
        )
        row = await r.single()
        n = int(row.get("n") or 0) if row else 0
    if n <= 0:
        return 0.5
    return min(1.0, 0.5 + 0.2 * n)
