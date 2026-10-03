"""След разбора ответа модели (`--explain`): исход каждой проверки по каждой роли.
Образец «Лизы»: резюме говорит о руководителе, а роли пусты, потому что модель процитировала только
реплики самого начальника. Данные синтетические."""
from __future__ import annotations

import json

from vera_shared.graph.pair_roles_parse import parse_answer, parse_traced
from vera_shared.graph.pair_roles_prompt import PROMPT
from vera_shared.graph.pair_roles_trace import DROPPED, KEPT, explain_payload
from vera_shared.graph.pair_roles_types import PackMessage, PairInference

MESSAGES = [
    PackMessage("m1", "2026-03-01", "dm", "B", "Прошу подготовить отчёт до пятницы"),
    PackMessage("m2", "2026-03-02", "dm", "A", "Отправил, готово, Вы просили сегодня"),
    PackMessage("m3", "2026-03-03", "mention", "X", "Наш директор сказал подготовить регламент"),
]
CORPUS = " ".join(m.text for m in MESSAGES) + " boss@corp.example"


def answer(*roles: dict) -> str:
    return json.dumps({"roles": list(roles), "relationship_summary": "руководитель"}, ensure_ascii=False)


def role(quotes, predicate="boss_of", subject="B", confidence=0.9, joke=False) -> dict:
    return {"predicate": predicate, "subject": subject, "confidence": confidence, "rationale": "x",
            "quotes": list(quotes), "joke_or_irony_only": joke}


def trace_of(*roles: dict):
    return parse_traced(answer(*roles), CORPUS, MESSAGES)


def test_self_assertion_is_named_with_quote_authors_and_the_summary_survives():
    roles, summary, (t,) = trace_of(role(["Прошу подготовить отчёт до пятницы"]))
    assert roles == [] and summary == "руководитель"
    assert (t.verdict, t.self_assertion) == (DROPPED, "самоутверждение")
    assert t.quotes[0].authors == ("B",) and t.quotes[0].found and not t.quotes[0].structural
    assert "сам «старший»" in t.reason and t.raw["predicate"] == "boss_of"


def test_corroboration_by_the_other_party_keeps_the_role_and_says_why():
    roles, _, (t,) = trace_of(role(["Прошу подготовить отчёт до пятницы", "Отправил, готово, Вы просили"]))
    assert [r.predicate for r in roles] == ["boss_of"]
    assert t.verdict == KEPT and t.self_assertion.startswith("подтверждена") and t.reason == "принята"
    assert t.quotes[1].authors == ("A",)


def test_every_drop_reason_is_reported_per_role():
    _, _, traces = trace_of(
        role(["Прошу подготовить отчёт до пятницы"], joke=True),
        role(["выдуманная цитата которой нет"]),
        role(["короткая"]),
        role(["Прошу подготовить отчёт до пятницы"], confidence=0.55, predicate="coworker_of", subject="both"),
        role(["boss@corp.example"], predicate="client_of", subject="A"),
        role(["Прошу подготовить отчёт до пятницы"], predicate="emperor_of"),
    )
    reasons = [t.reason for t in traces]
    assert "шутка" in reasons[0] and traces[0].joke
    assert "ни одной цитаты" in reasons[1]
    assert traces[1].quotes[0].found is False
    assert traces[2].quotes[0].short is True and "ни одной цитаты" in reasons[2]
    assert "ниже порога" in reasons[3] and traces[3].confidence == 0.55
    assert traces[4].verdict == KEPT and traces[4].quotes[0].structural
    assert "неизвестный предикат" in reasons[5]


def test_rival_hierarchy_duplicates_and_the_cap_are_explained():
    q = ["Прошу подготовить отчёт до пятницы", "Отправил, готово, Вы просили"]
    roles, _, (a, b, dup1, dup2) = trace_of(
        role(q, subject="A", confidence=0.7), role(q, subject="B", confidence=0.9),
        role(q, predicate="friend_of", subject="both", confidence=0.7),
        role(q, predicate="friend_of", subject="both", confidence=0.8))
    assert [(r.predicate, r.direction) for r in roles] == [("boss_of", "b_to_a"), ("friend_of", "both")]
    assert "противоположная сторона" in a.reason and b.verdict == KEPT
    assert "повтор" in dup1.reason and dup2.verdict == KEPT


def test_the_owners_own_instructions_confirm_a_role_when_he_is_the_superior():
    own = role(["Прошу подготовить отчёт до пятницы"])                 # цитата автора B, B — «над»
    roles, _, (t,) = parse_traced(answer(own), CORPUS, MESSAGES, owner="B")
    assert [r.predicate for r in roles] == ["boss_of"] and t.verdict == KEPT
    assert "цитаты владельца — доверенный источник" in t.self_assertion


def test_the_rule_stays_strict_for_a_non_owner_superior_and_for_the_wrong_owner_side():
    own = role(["Прошу подготовить отчёт до пятницы"])
    assert parse_traced(answer(own), CORPUS, MESSAGES)[0] == []                    # пара без владельца
    assert parse_traced(answer(own), CORPUS, MESSAGES, owner="A")[0] == []         # владелец — подчинённый, сказал не он


def test_the_owners_word_alone_does_not_make_him_a_parent():
    """Доверие к словам владельца — только для рабочей иерархии: одно его «сынок»
    не делает его родителем (ревью 04.10.2026)."""
    own = role(["Прошу подготовить отчёт до пятницы"], predicate="parent_of")
    assert parse_traced(answer(own), CORPUS, MESSAGES, owner="B")[0] == []


def test_the_owner_as_subordinate_is_the_other_party():
    reports = role(["Отправил, готово, Вы просили"], subject="B")                  # владелец A отчитывается
    roles, _, (t,) = parse_traced(answer(reports), CORPUS, MESSAGES, owner="A")
    assert [r.predicate for r in roles] == ["boss_of"]
    assert t.self_assertion.startswith("подтверждена") and "владельца" not in t.self_assertion


def test_the_traced_and_the_plain_parser_agree():
    raw = answer(role(["Прошу подготовить отчёт до пятницы", "Отправил, готово, Вы просили"]))
    assert parse_answer(raw, CORPUS, MESSAGES) == parse_traced(raw, CORPUS, MESSAGES)[:2]


def test_the_explain_payload_is_json_ready_and_shows_the_model_output():
    _, _, traces = trace_of(role(["Прошу подготовить отчёт до пятницы"]))
    out = explain_payload(PairInference(1, 2, trace=tuple(traces)))
    text = json.dumps(out, ensure_ascii=False)
    assert out[0]["model_returned"]["predicate"] == "boss_of" and out[0]["verdict"] == "dropped"
    assert out[0]["quotes"][0]["status"] == "найдена" and "авторы: B" in text and "самоутверждение" in text


def test_the_prompt_asks_for_a_quote_from_the_other_party_for_hierarchy_roles():
    assert "MUST include at least one line written by the OTHER person" in PROMPT
    assert "below 0.6" in PROMPT
