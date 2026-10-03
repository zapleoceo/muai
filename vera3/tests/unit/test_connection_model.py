"""Связь как пара: вес роли по записям и общению, вывод «работает с», пороги. Без базы."""
from __future__ import annotations

from datetime import datetime

import pytest
from vera_shared.graph import connection_model as m
from vera_shared.graph.pair_stats import PairStats

A, B = 1, 2


def claim(predicate: str, *, s: int = A, o: int = B, conf: float = 0.8, manual: bool = False,
          fact: str | None = None, day: int = 1, rel_id: int | None = None) -> m.Claim:
    return m.Claim(s, predicate, o, conf, manual, fact, datetime(2026, 9, day), rel_id)


def stats(**kw: int) -> PairStats:
    return PairStats(**kw)


def test_interaction_saturates_with_contact_days():
    assert m.interaction_strength(stats()) == 0
    assert 0.3 < m.interaction_strength(stats(active_days=4)) < 0.45
    assert m.interaction_strength(stats(active_days=30)) > 0.95


def test_small_shared_groups_count_for_half_a_day_and_are_capped():
    four = m.interaction_strength(stats(shared_groups=4))
    assert four == m.interaction_strength(stats(active_days=2))
    assert m.interaction_strength(stats(shared_groups=40)) == four


def test_established_threshold_is_about_eight_days():
    assert not m.is_established(stats(active_days=6))
    assert m.is_established(stats(active_days=8))


def test_one_phrase_without_contact_is_a_weak_role():
    conn = m.build_connection(A, B, [claim("boss_of")], stats())
    assert conn is not None
    assert conn.weight == pytest.approx(0.4)
    assert conn.main.support == 1 and not conn.main.manual and not conn.main.inferred


def test_dense_contact_corroborates_the_asserted_role():
    quiet = m.build_connection(A, B, [claim("boss_of")], stats())
    busy = m.build_connection(A, B, [claim("boss_of")], stats(active_days=30))
    assert busy.weight > quiet.weight * 1.8


def test_more_supporting_records_raise_the_weight():
    one = m.build_connection(A, B, [claim("client_of")], stats())
    two = m.build_connection(A, B, [claim("client_of"), claim("client_of")], stats())
    assert two.weight > one.weight and two.main.support == 2


def test_manual_claim_is_full_weight_regardless_of_contact():
    conn = m.build_connection(A, B, [claim("boss_of", manual=True, conf=0.1)], stats())
    assert conn.weight == m.MANUAL_WEIGHT and conn.main.manual


def test_symmetric_and_inverse_forms_fold_into_one_role():
    conn = m.build_connection(A, B, [claim("coworker_of"), claim("coworker_of", s=B, o=A)],
                              stats())
    assert len(conn.roles) == 1 and conn.main.support == 2 and conn.main.subject_id is None
    chain = m.build_connection(A, B, [claim("boss_of"), claim("reports_to", s=B, o=A)], stats())
    assert len(chain.roles) == 1 and chain.main.predicate == "boss_of"
    assert chain.main.subject_id == A


def test_opposite_hierarchy_directions_stay_separate_roles():
    conn = m.build_connection(A, B, [claim("boss_of"), claim("boss_of", s=B, o=A, conf=0.5)],
                              stats())
    assert len(conn.roles) == 2 and conn.main.subject_id == A


def test_secondary_roles_shown_only_above_floor_and_ratio():
    claims = [claim("boss_of", conf=1.0), claim("client_of", conf=0.9),
              claim("vendor_of", conf=0.2)]
    conn = m.build_connection(A, B, claims, stats())
    assert conn.main.predicate == "boss_of"
    assert [r.predicate for r in conn.also] == ["client_of"]
    assert conn.hidden == 1


def test_four_single_phrase_roles_collapse_to_one_main_and_hidden_rest():
    claims = [claim("boss_of", conf=0.9), claim("client_of", conf=0.6),
              claim("coworker_of", conf=0.5), claim("vendor_of", conf=0.3)]
    conn = m.build_connection(A, B, claims, stats())
    assert conn.main.predicate == "boss_of"
    assert len(conn.also) <= 1 and conn.hidden >= 2


def test_ties_prefer_the_more_specific_role():
    conn = m.build_connection(A, B, [claim("coworker_of"), claim("boss_of")], stats())
    assert conn.main.predicate == "boss_of"


def test_latest_fact_and_best_confidence_are_reported():
    conn = m.build_connection(A, B, [claim("boss_of", fact="старое", day=1, conf=0.9),
                                     claim("boss_of", fact="новое", day=5, conf=0.6)], stats())
    assert conn.main.fact == "новое" and conn.main.confidence == 0.9


def test_no_claims_and_no_work_signal_is_no_connection():
    assert m.build_connection(A, B, [], stats(active_days=40)) is None


def test_work_together_is_inferred_from_shared_domain_and_contact():
    conn = m.build_connection(A, B, [], stats(active_days=20), shared_work=True)
    assert conn.main.predicate == "coworker_of" and conn.main.inferred
    assert conn.main.support == 0 and 0.5 < conn.weight <= m.INFER_MAX


def test_inference_needs_enough_days_for_the_signal_kind():
    assert m.build_connection(A, B, [], stats(active_days=2), shared_work=True) is None
    assert m.build_connection(A, B, [], stats(active_days=5, work_co_days=5)) is None
    chat_only = m.build_connection(A, B, [], stats(active_days=6, work_co_days=6))
    assert chat_only is not None and chat_only.main.inferred


def test_shared_domain_with_no_contact_infers_nothing():
    assert m.build_connection(A, B, [], stats(), shared_work=True) is None


def test_inferred_work_strengthens_an_asserted_coworker_role():
    asserted = m.build_connection(A, B, [claim("coworker_of")], stats(active_days=20))
    both = m.build_connection(A, B, [claim("coworker_of")], stats(active_days=20),
                              shared_work=True)
    assert both.weight > asserted.weight and both.main.support == 1 and both.main.inferred


def test_inference_does_not_override_a_different_main_role():
    conn = m.build_connection(A, B, [claim("boss_of", manual=True)], stats(active_days=20),
                              shared_work=True)
    assert conn.main.predicate == "boss_of"
    assert [r.predicate for r in conn.roles] == ["boss_of", "coworker_of"]


def test_role_payload_direction_is_relative_to_the_viewer():
    conn = m.build_connection(A, B, [claim("boss_of", rel_id=7)], stats())
    assert m.role_payload(conn.main, A)["direction"] == "out"
    payload = m.role_payload(conn.main, B)
    assert payload["direction"] == "in" and payload["rel_ids"] == [7]
    sym = m.build_connection(A, B, [claim("friend_of")], stats())
    assert m.role_payload(sym.main, A)["direction"] == "both"
    assert "rel_ids" not in m.role_payload(sym.main, A)
