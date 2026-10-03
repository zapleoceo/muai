"""rel_canon: каноническая форма, эквиваленты и сила связи."""
from __future__ import annotations

import pytest
from vera_shared.graph.rel_canon import (
    canonical_edge,
    equivalent_forms,
    rival_forms,
    strength,
)


@pytest.mark.parametrize("predicate", ["coworker_of", "friend_of", "spouse_of"])
def test_symmetric_is_ordered_by_id(predicate):
    assert canonical_edge(9, predicate, 3) == (3, predicate, 9)
    assert canonical_edge(3, predicate, 9) == (3, predicate, 9)


def test_inverse_predicates_become_active_form():
    assert canonical_edge(1, "reports_to", 2) == (2, "boss_of", 1)
    assert canonical_edge(1, "child_of", 2) == (2, "parent_of", 1)
    assert canonical_edge(1, "boss_of", 2) == (1, "boss_of", 2)


def test_other_predicates_untouched():
    assert canonical_edge(9, "works_at", 3) == (9, "works_at", 3)


def test_equivalents_cover_legacy_storage():
    assert equivalent_forms(9, "friend_of", 3) == [(3, "friend_of", 9), (9, "friend_of", 3)]
    assert (1, "reports_to", 2) in equivalent_forms(2, "boss_of", 1)


def test_rivals_only_for_antisymmetric():
    assert rival_forms(1, "boss_of", 2) == [(2, "boss_of", 1), (1, "reports_to", 2)]
    assert rival_forms(1, "works_at", 2) == []
    assert rival_forms(1, "coworker_of", 2) == []


def test_strength_prefers_manual_then_confidence():
    manual = {"derived_from_event_id": None, "confidence": 0.5, "fact": ""}
    auto = {"derived_from_event_id": 5, "confidence": 0.99, "fact": "long fact"}
    assert strength(manual) > strength(auto)
    assert strength({**auto, "confidence": 0.9}) < strength(auto)
