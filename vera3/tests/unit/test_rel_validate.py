"""vera_shared.graph.rel_validate — связь проверяется до записи.

Кейсы — живой мусор с прода (до 2026-09-03, mistral-small): каждый обязан
отсеиваться, а нормальные связи — проходить.
"""
from __future__ import annotations

import pytest
from vera_shared.graph.rel_validate import (
    REJECT_LOW_CONFIDENCE,
    REJECT_NOT_REFERENTIAL,
    REJECT_SELF,
    REJECT_SERVICE_ACCOUNT,
    REJECT_TYPE,
    is_service_name,
    normalized_name,
    relationship_reject_reason,
    same_person_name,
)

P, ORG, G = "person", "organization", "supergroup"


def reason(subj, st, pred, obj, ot, conf=0.9):
    return relationship_reject_reason(
        subject_name=subj, subject_type=st, predicate=pred,
        object_name=obj, object_type=ot, confidence=conf)


@pytest.mark.parametrize(("subj", "st", "pred", "obj", "ot", "why"), [
    ("Olga Kryachko / Sintegrum", P, "works_at", "Olga Kryachko", P, REJECT_SELF),
    ("Ольга Крячко (JIRA)", P, "works_at", "Olga Kryachko", ORG, REJECT_SELF),
    ("Ольга Крячко (JIRA)", P, "works_at", "Artem Belov", P, REJECT_TYPE),
    ("Ольга Крячко (JIRA)", P, "reports_to", "Vadim Kudryavtsev", P, REJECT_SERVICE_ACCOUNT),
    ("Link", P, "works_at", "OpenRouter, Inc", P, REJECT_TYPE),
    ("OpenRouter Team", P, "works_at", "OpenRouter", ORG, REJECT_SERVICE_ACCOUNT),
    ("Arthur Pavlechko", P, "works_at", "Українці у Вʼєтнамі", G, REJECT_TYPE),
    ("Slack", ORG, "coworker_of", "Dima Zaporozhets Dmytro", P, REJECT_TYPE),
    ("Dima", P, "lives_in", "Олег", P, REJECT_TYPE),
    ("Inna", P, "friend_of", "Inna", P, REJECT_SELF),
    ("Ли", P, "coworker_of", "он", P, REJECT_NOT_REFERENTIAL),
    ("Dima Zaporozhets Dmytro", P, "works_at", "Dmitriy Zaporozhets", ORG, REJECT_SELF),
])
def test_prod_junk_is_rejected(subj, st, pred, obj, ot, why):
    assert reason(subj, st, pred, obj, ot) == why


@pytest.mark.parametrize(("subj", "st", "pred", "obj", "ot"), [
    ("Игорь Нерозя", P, "coworker_of", "Artem Belov", P),
    ("Иван Петров", P, "works_at", "IT STEP", ORG),
    ("Дмитро Корчевський", P, "co_founder_of", "IT STEP", ORG),
    ("Veranda", ORG, "client_of", "Ли Визардиум", P),
    ("Оля", P, "spouse_of", "Максим", P),
    ("Дима", P, "lives_in", "Нячанг", ORG),
])
def test_sane_relationships_pass(subj, st, pred, obj, ot):
    assert reason(subj, st, pred, obj, ot) is None


def test_low_confidence():
    assert reason("Оля", P, "friend_of", "Максим", P, conf=0.3) == REJECT_LOW_CONFIDENCE


def test_unknown_predicate_is_type_mismatch():
    assert reason("Оля", P, "enemy_of", "Максим", P) == REJECT_TYPE


def test_normalized_name():
    assert normalized_name("  Olga  Kryachko / Sintegrum") == "olga kryachko"
    assert normalized_name("Ольга Крячко (JIRA)") == "ольга крячко"
    assert normalized_name(None) == ""


def test_same_person_name():
    assert same_person_name("Маша Иванова", "Maria Ivanova") is True
    assert same_person_name("Оля", "Олег") is False
    assert same_person_name("(JIRA)", "Olga") is False


@pytest.mark.parametrize(("name", "service"), [
    ("Vadim Kudryavtsev (JIRA)", True), ("Manus Team", True), ("noreply", True),
    ("benchkiller_bot", True), ("Jon Talbot", False), ("Olga Kryachko", False),
    (None, False),
])
def test_is_service_name(name, service):
    assert is_service_name(name) is service
