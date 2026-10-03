"""rel_text и проверка свидетельств в rel_validate."""
from __future__ import annotations

import pytest
from vera_shared.graph.rel_policy import has_relation_marker
from vera_shared.graph.rel_text import End, Evidence, mentions, single_token_name
from vera_shared.graph.rel_validate import (
    REJECT_FACT,
    REJECT_WEAK_NAME,
    TOOL_TAG_RE,
    relationship_reject_reason,
)

P = "person"


def ev(fact, subj, obj, strong_subject=False):
    return Evidence(fact, End(tuple(subj), strong=strong_subject), End(tuple(obj)))


def reason(subj, obj, evidence, pred="coworker_of"):
    return relationship_reject_reason(
        subject_name=subj, subject_type=P, predicate=pred, object_name=obj,
        object_type=P, confidence=0.9, evidence=evidence)


@pytest.mark.parametrize(("name", "single"), [
    ("Андрей", True), ("Андрей (JIRA)", True), ("Anna Lee", False),
    ("Anna / Org", True), ("Анна Иванова", False),
])
def test_single_token_name(name, single):
    assert single_token_name(name) is single


@pytest.mark.parametrize(("fact", "names", "expected"), [
    ("Позвони Андрею завтра", ["Andrey Petrov"], True),
    ("Настя написала мне", ["Настя Ким"], True),
    ("Viktor Gavrylenko wrote", ["Виктор Гавриленко"], True),
    ("совсем другой текст", ["Андрей Петров"], False),
    ("Ли пришёл", ["Ли"], True),
    ("the group is here", ["Group Inc"], False),
    (None, ["Андрей Петров"], False),
])
def test_mentions(fact, names, expected):
    assert mentions(fact, End(tuple(names))) is expected


def test_author_mentioned_by_first_person():
    assert mentions("я работаю с Олей", End(("Dima Z",), strong=True, author=True))
    assert not mentions("я работаю с Олей", End(("Dima Z",)))


def test_weak_single_name_rejected_unless_strong():
    fact = "Андрей и Anna Lee работают вместе"
    assert reason("Андрей", "Anna Lee", ev(fact, ["Андрей"], ["Anna Lee"])) == REJECT_WEAK_NAME
    strong = ev(fact, ["Андрей"], ["Anna Lee"], strong_subject=True)
    assert reason("Андрей", "Anna Lee", strong) is None


def test_fact_must_name_both_ends():
    assert reason("Ivan Petrov", "Anna Lee",
                  ev("Anna Lee работает тут", ["Ivan Petrov"], ["Anna Lee"])) == REJECT_FACT
    assert reason("Ivan Petrov", "Anna Lee",
                  ev("Ivan Petrov и Anna Lee", ["Ivan Petrov"], ["Anna Lee"])) is None


def test_without_evidence_old_behaviour():
    assert reason("Андрей", "Anna Lee", None) is None


def test_org_single_token_allowed():
    evidence = ev("Anna Lee работает в Google", ["Anna Lee"], ["Google"])
    assert relationship_reject_reason(
        subject_name="Anna Lee", subject_type=P, predicate="works_at",
        object_name="Google", object_type="organization", confidence=0.9,
        evidence=evidence) is None


def test_bare_tim_no_longer_leaks_and_tool_tag_shared():
    for text in ("the timeline is long", "Timur wrote about timing"):
        assert has_relation_marker(text) is False
    assert has_relation_marker("dia bergabung dengan tim kami")
    assert TOOL_TAG_RE.search("Anna (Google Calendar)")
    assert TOOL_TAG_RE.search("Anna ( JIRA )")
