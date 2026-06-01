"""v3 decision dispatcher.

  Event arrives → query graph (L1+L2+L3) → enumerate candidate actions
  → score each via decide.scoring → choose by band → return Decision.

Candidate enumeration:
  - context-matched patterns (same entity shape, confirmed by Dima)
  - fallback to globally confirmed patterns when context is sparse
  - always-present 'ask' fallback (score → user prompt)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.brain import patterns as P
from app.decide.scoring import Candidate, Scored, score

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Decision:
    band: str            # 'auto' | 'propose' | 'ask' | 'silent'
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

    scored = [await score(c, event_hints) for c in cands]
    scored.sort(key=lambda s: s.score, reverse=True)

    top = scored[0]
    band = _band(top.score)
    reason = (f"top={top.candidate.label} score={top.score:.2f} "
              f"({_summarise(top.breakdown)})")
    return Decision(band=band, chosen=top, candidates=scored, reason=reason)


async def _enumerate_candidates(hints: list[dict]) -> list[Candidate]:
    """Brain candidates from Pattern nodes, scoped to this event's context.

    Context-specific patterns come first; if the brain has none for this
    context yet, global confirmed patterns serve as a fallback so the
    scoring pipeline always has something to evaluate.
    """
    ctx = P.context_key_for(hints)
    try:
        pattern_rows = await P.get_candidates(ctx, limit=5)
    except Exception as exc:
        log.warning("Pattern candidate lookup failed: %s", exc)
        pattern_rows = []

    cands: list[Candidate] = []
    for row in pattern_rows:
        cands.append(Candidate(
            label=row.get("action_label") or "?",
            tool=row.get("tool"),
            args=row.get("args"),           # already parsed to dict by get_candidates
            rationale=f"pattern: {row.get('confirmation_count', 0)} confirmations",
        ))

    cands.append(_ASK_CANDIDATE)
    return cands


def _band(s: float) -> str:
    """Decision band from alignment score (0..10).
       auto    — fire immediately, post-fact card
       propose — show a card for user approval
       silent  — write status, no card

    Thresholds tuned 2026-06-01 after user complained that score-3.8 cards
    ('alignment=3.8, предлагаю') flooded the group. Old: 3.0/7.0. New 5.0/8.0
    silences low-confidence proposals; user can lower if needed.
    """
    if s >= 8.0:
        return "auto"
    if s >= 5.0:
        return "propose"
    return "silent"


def _summarise(breakdown: dict) -> str:
    keys = ("value_alignment", "goal_contribution", "pattern_match", "reversibility")
    return ", ".join(f"{k}={breakdown[k]}" for k in keys if k in breakdown)
