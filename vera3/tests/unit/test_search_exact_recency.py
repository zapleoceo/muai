"""Точные тикет-попадания: при равном rank лимит оставляет самые свежие."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain_search.identifier_rows import IDENTIFIER_RANK
from brain_search.rows import Candidate
from brain_search.scoring import score_candidates

_BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _cand(event_id: int, days: int, *, rank: float, importance: int,
          vec_sim: float | None) -> Candidate:
    return Candidate(id=event_id, source="gmail", source_event_id=f"s{event_id}",
                     occurred_at=_BASE + timedelta(days=days), content_text="SIN-4885",
                     importance=importance, embedding=None, rank=rank, account="a",
                     vec_sim=vec_sim)


def test_limit_keeps_newest_exact_hits_newest_first():
    exact = [
        _cand(1, 0, rank=IDENTIFIER_RANK, importance=95, vec_sim=0.9),
        _cand(2, 1, rank=IDENTIFIER_RANK, importance=90, vec_sim=0.8),
        _cand(3, 2, rank=IDENTIFIER_RANK, importance=80, vec_sim=0.7),
        _cand(4, 3, rank=IDENTIFIER_RANK, importance=70, vec_sim=0.6),
        _cand(5, 4, rank=IDENTIFIER_RANK, importance=60, vec_sim=0.5),
        _cand(6, 5, rank=IDENTIFIER_RANK, importance=20, vec_sim=0.1),
        _cand(7, 8, rank=IDENTIFIER_RANK, importance=10, vec_sim=None),
    ]
    semantic = _cand(8, 9, rank=0.5, importance=100, vec_sim=0.99)
    ranked = score_candidates([semantic, *exact], [0.1], [])[:5]
    assert [c.id for _, c in ranked] == [7, 6, 5, 4, 3]


def test_exact_hits_stay_above_semantic_ones():
    exact = _cand(1, 0, rank=IDENTIFIER_RANK, importance=1, vec_sim=None)
    semantic = _cand(2, 9, rank=0.9, importance=100, vec_sim=0.99)
    ranked = score_candidates([semantic, exact], [0.1], [])
    assert [c.id for _, c in ranked] == [1, 2]
