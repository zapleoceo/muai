"""Чистые части связей событий: исправление ASR-имён, формы имён, разрешение имени в круге,
SQL-фильтр, сборка связей по метаданным. Данные синтетические."""
from __future__ import annotations

from datetime import datetime

import pytest
from vera_shared.links.asr import MAX_CONFIDENCE, asr_matches
from vera_shared.links.builders import (
    base_links,
    is_anonymous,
    speakers_of,
    voice_links,
)
from vera_shared.links.context import EventFacts, EventView
from vera_shared.links.filters import (
    EventFilter,
    FilterError,
    and_clause,
    build_where,
    from_dict,
    to_dict,
)
from vera_shared.links.matcher import PersonNames
from vera_shared.links.model import (
    ALIAS,
    AUTHOR,
    MENTIONED,
    NAME_MATCH,
    PARTICIPANT,
    RECIPIENT,
)
from vera_shared.links.name_forms import name_group
from vera_shared.links.names import NameResolver
from vera_shared.links.scope import ChatContext

CANDIDATES = {1: "Виктор Корчагин", 2: "Лиза Ветрова", 3: "Иван Ковальчук"}


def test_asr_fixes_a_mangled_surname_with_lowered_confidence():
    (hit,) = asr_matches("Арчагин сказал, что отчёт готов", CANDIDATES)
    assert (hit.entity_id, hit.heard) == (1, "Арчагин")
    assert 0.4 <= hit.confidence <= MAX_CONFIDENCE


def test_asr_leaves_exact_names_and_unrelated_words_alone():
    assert asr_matches("Корчагин и Ветрова здесь", CANDIDATES) == []
    assert asr_matches("Сегодняшний разговор", CANDIDATES) == []
    assert asr_matches("арчагин в нижнем регистре", CANDIDATES) == []


def test_asr_refuses_when_two_candidates_are_equally_close():
    twins = {1: "Виктор Корчагин", 2: "Олег Корчагин"}
    assert asr_matches("Арчагин сказал", twins) == []


def test_name_forms_group_diminutives_across_alphabets():
    assert name_group("dima") == name_group("dmitri") == name_group("dim")
    assert name_group("dima") != name_group("igor")


PEOPLE = [PersonNames(1, "Dmitry Testov"), PersonNames(2, "Дима"), PersonNames(3, "Лиза Ветрова"),
          PersonNames(4, "Лиза Ветрова")]


def test_resolver_full_name_is_global_and_unique():
    r = NameResolver(PEOPLE)
    assert r.full("Testov Dmitry") == 1          # порядок слов неважен
    assert r.full("Лиза Ветрова") is None         # двое с одним полным именем — неоднозначно


def test_resolver_short_name_needs_the_circle():
    r = NameResolver(PEOPLE)
    assert r.short("Дима", {1, 3}) == 1
    assert r.short("Дима", {2, 3}) == 2
    assert r.short("Дима", {1, 2}) is None
    assert r.short("Дима", {3}) is None
    assert r.resolve("Дима", {1, 3}) == (1, "short")
    assert r.matches("Дима", {1, 2}) and not r.matches("Дима", {3})


def test_filter_combines_everything_through_and():
    f = EventFilter(participant_ids=(5, 6), mentioned_ids=(7,), with_owner=True, kind="call",
                    start=datetime(2026, 9, 1), end=datetime(2026, 10, 1), project="itstep")
    where, params = build_where(f, owner_id=1)
    assert where.count("EXISTS") == 4 and " AND " in where
    assert "events.source IN (:s0)" in where and params["s0"] == "voice"
    assert params["p0"] == 5 and params["owner"] == 1 and params["f_project"] == "itstep"
    assert "l.scope_ok" in where and "l.confidence >= :link_min" in where


def test_filter_validates_input():
    with pytest.raises(FilterError):
        build_where(EventFilter(with_owner=True), owner_id=None)
    with pytest.raises(FilterError):
        build_where(EventFilter(kind="video"), owner_id=1)
    with pytest.raises(FilterError):
        build_where(EventFilter(participant_ids=tuple(range(11))), owner_id=1)
    assert build_where(EventFilter(kind="call", source="gmail"), 1)[0] == "1 = 0"
    assert and_clause(None, 1) == ("", {})
    assert and_clause(EventFilter(), 1) == ("", {})


def test_filter_survives_json_round_trip_and_rejects_unknown_keys():
    f = EventFilter(participant_ids=(1,), kind="email", start=datetime(2026, 9, 1), min_confidence=0.5)
    assert from_dict(to_dict(f)) == f
    with pytest.raises(FilterError):
        from_dict({"participants": [1]})


def view(source: str, **meta) -> EventView:
    return EventView(10, source, "", meta)


def test_base_links_give_author_and_recipients_by_alias():
    links = base_links(view("gmail"), EventFacts(author=1, recipients=(2, 3)))
    assert [(x.entity_id, x.role, x.source) for x in links] == [
        (1, AUTHOR, ALIAS), (2, RECIPIENT, ALIAS), (3, RECIPIENT, ALIAS)]
    assert base_links(view("telegram"), EventFacts()) == []


def test_voice_links_skip_anonymous_speakers_and_the_owner_as_participant():
    v = EventView(11, "voice", "", {"voices": ["Лиза Ветрова", "Собеседник 2"],
                                    "counterparts": ["Иван Ковальчук"]},
                  {"utterances": [{"speaker": "Лиза Ветрова", "text": "a"},
                                  {"speaker": "Лиза Ветрова", "text": "b"},
                                  {"speaker": "Собеседник 2", "text": "c"},
                                  {"speaker": "Эхо", "text": "d", "echo": True}]})
    assert speakers_of(v) == {"Лиза Ветрова": 2, "Собеседник 2": 1}
    assert is_anonymous("собеседник 3") and not is_anonymous("Лиза")
    known = {"Лиза Ветрова": (3, "voiceprint", 0.9)}
    links = voice_links(v, 1, known.get, lambda name: 5 if name == "Иван Ковальчук" else None,
                        asr_matches("Арчагин", CANDIDATES))
    by = {(x.entity_id, x.role): x for x in links}
    assert by[(1, AUTHOR)].source == ALIAS
    assert by[(3, PARTICIPANT)].span == {"speaker": "Лиза Ветрова", "utterances": 2}
    assert by[(5, PARTICIPANT)].source == NAME_MATCH
    assert by[(1, MENTIONED)].span["asr"] is True      # угаданное по искажённому имени
    assert len([x for x in links if x.role == PARTICIPANT]) == 2    # «Собеседник 2» связи не даёт


def test_chat_context_defaults_are_empty():
    ctx = ChatContext()
    assert not ctx.participants and not ctx.extended and ctx.dm_partner is None
