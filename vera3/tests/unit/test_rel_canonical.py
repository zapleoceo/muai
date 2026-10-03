"""Проход `canonical` чистки и защита от «(это я)»: чистые функции по срезу. Имена синтетические."""
from __future__ import annotations

from vera_shared.graph.rel_cleanup import plan_document
from vera_shared.graph.rel_cleanup_canonical import RULE_KEPT_EVIDENCE, canonical_plan
from vera_shared.graph.rel_text import (
    End,
    Evidence,
    claims_to_be_author,
    has_self_marker,
)
from vera_shared.graph.rel_validate import (
    REJECT_SELF_REFERENCE,
    relationship_reject_reason,
)


def row(rel_id, s, p, o, *, event=1, current=True, conf=0.8):
    return {"id": rel_id, "subject_entity_id": s, "predicate": p, "object_entity_id": o,
            "confidence": conf, "fact": "f", "is_current": current,
            "derived_from_event_id": event, "subject_name": f"P{s}", "subject_type": "person",
            "object_name": f"P{o}", "object_type": "person"}


def plan(*rows):
    return canonical_plan({"owner_id": 9, "relationships": list(rows), "names": {}})


def kinds(actions):
    return sorted((a["rel_id"], a["action"], a["rule"]) for a in actions)


def test_reports_to_and_child_of_are_converted_even_for_single_word_names():
    actions = plan(row(1, 1, "reports_to", 2), row(2, 3, "child_of", 4))
    assert kinds(actions) == [(1, "convert", "convert_inverse"), (2, "convert", "convert_inverse")]
    after = {a["rel_id"]: a["after"] for a in actions}
    assert after[1]["subject_entity_id"] == 2 and after[1]["predicate"] == "boss_of"
    assert after[2]["predicate"] == "parent_of" and after[2]["subject_entity_id"] == 4


def test_symmetric_rows_get_min_max_order():
    (action,) = plan(row(1, 7, "coworker_of", 3))
    assert (action["after"]["subject_entity_id"], action["after"]["object_entity_id"]) == (3, 7)


def test_already_canonical_rows_and_retired_rows_are_left_alone():
    assert plan(row(1, 1, "boss_of", 2), row(2, 3, "coworker_of", 4),
                row(3, 9, "reports_to", 8, current=False)) == []


def test_duplicate_from_the_same_event_is_retired_but_other_events_are_kept_as_evidence():
    same_event = plan(row(1, 1, "coworker_of", 2, event=5), row(2, 2, "coworker_of", 1, event=5))
    assert kinds(same_event) == [(2, "retire", "symmetric_duplicate")]
    other_event = plan(row(1, 1, "coworker_of", 2, event=5), row(2, 2, "coworker_of", 1, event=6))
    assert kinds(other_event) == [(2, "skip", RULE_KEPT_EVIDENCE)]


def test_inverse_duplicate_keeps_the_stronger_and_only_one_conversion_is_planned():
    actions = plan(row(1, 2, "reports_to", 1, event=5, conf=0.9),
                   row(2, 2, "reports_to", 1, event=5, conf=0.5))   # невозможно по UNIQUE, но план устойчив
    assert [a["rel_id"] for a in actions if a["action"] == "convert"] == [1]
    two_events = plan(row(1, 2, "reports_to", 1, event=5), row(2, 1, "boss_of", 2, event=6))
    assert [a["action"] for a in two_events] == ["skip"]            # каноническая тройка занята


def test_two_way_hierarchy_retires_the_side_with_fewer_distinct_events():
    actions = plan(row(1, 1, "boss_of", 2, event=10), row(2, 1, "boss_of", 2, event=11),
                   row(3, 2, "boss_of", 1, event=12))
    assert (3, "retire", "contradiction") in kinds(actions)
    assert not [a for a in actions if a["rel_id"] in (1, 2) and a["action"] == "retire"]


def test_two_way_hierarchy_tie_keeps_both():
    actions = plan(row(1, 1, "boss_of", 2, event=10), row(2, 2, "boss_of", 1, event=11))
    assert not [a for a in actions if a["action"] == "retire"]


def test_two_way_hierarchy_manual_side_wins_over_extracted_events():
    actions = plan(row(1, 1, "boss_of", 2, event=None),
                   row(2, 2, "boss_of", 1, event=10), row(3, 2, "boss_of", 1, event=11))
    assert sorted(a["rel_id"] for a in actions if a["action"] == "retire") == [2, 3]


def test_plan_document_records_the_canonical_phase():
    doc = plan_document(plan(row(1, 1, "reports_to", 2)), "db", "canonical")
    assert doc["phase"] == "canonical" and doc["to_apply"] == 1


FACT = "На Андрея (это я) тоже наложили штраф"


def test_self_marker_detection():
    assert has_self_marker(FACT) and has_self_marker("I am Andrey")
    assert not has_self_marker("Андрей сказал, что я опоздал")


def test_named_end_next_to_the_marker_is_the_author():
    assert claims_to_be_author(FACT, End(("Андрей",)))
    assert claims_to_be_author("Hi, I am Andrey from sales", End(("Andrey",)))
    assert not claims_to_be_author(FACT, End(("Сандра",)))


def _reject(author_flag: bool):
    ev = Evidence(FACT, End(("Андрей",), strong=author_flag, author=author_flag),
                  End(("Сандра",)))
    return relationship_reject_reason(
        subject_name="Андрей", subject_type="person", predicate="boss_of",
        object_name="Сандра", object_type="person", confidence=0.9, evidence=ev)


def test_self_reference_to_another_entity_is_rejected_not_attributed_to_a_namesake():
    assert _reject(False) == REJECT_SELF_REFERENCE


def test_self_reference_resolving_to_the_author_passes_the_check():
    assert _reject(True) != REJECT_SELF_REFERENCE


def test_possessive_or_other_clause_is_not_a_self_reference():
    sandra, john, anna, andrey = (End((n,)) for n in ("Sandra", "John", "Anna", "Андрей"))
    assert not claims_to_be_author("I am Sandra's boss", sandra)
    assert not claims_to_be_author("I'm John's brother", john)
    assert not claims_to_be_author("I am Anna’s friend", anna)
    assert not claims_to_be_author("Позвонил Андрей, это я опоздал", andrey)
    assert not claims_to_be_author("Андрей сказал, что это я виноват", andrey)


def test_direct_apposition_and_complement_are_self_references():
    assert claims_to_be_author("На Андрея (это я) наложили штраф", End(("Андрей",)))
    assert claims_to_be_author("Андрей — это я", End(("Андрей",)))
    assert claims_to_be_author("Андрей (я) тоже там был", End(("Андрей",)))
    assert claims_to_be_author("I am Sandra from sales", End(("Sandra",)))
    assert claims_to_be_author("I'm Andrey", End(("Andrey",)))


def test_contradiction_keep_is_the_strongest_row_regardless_of_input_order():
    rows = [row(1, 1, "boss_of", 2, event=10, conf=0.5), row(2, 1, "boss_of", 2, event=11, conf=0.9),
            row(3, 2, "boss_of", 1, event=12)]
    for ordering in (rows, rows[::-1]):
        retire = [a for a in plan(*ordering) if a["action"] == "retire"]
        assert [a["rel_id"] for a in retire] == [3] and retire[0]["keep_id"] == 2
