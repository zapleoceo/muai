"""rel_extract.is_referential_name — местоимение не человек.

Инцидент 2026-08-20: у телеграм-аккаунта 942121006 имя профиля буквально «он».
LLM доставала факты из фраз «Нормально он написал», resolve_entity_exact('он')
находила ровно одно совпадение — и факт прилипал к постороннему. 55 связей.
"""
from __future__ import annotations

import pytest
from vera_shared.graph.rel_extract import SELF_TOKENS, is_referential_name


@pytest.mark.parametrize("name", [
    "он", "Он", "  ОН  ", "она", "они", "его", "её", "их", "ты", "вы", "мы",
    "этот", "тот", "кто", "что",
    "he", "She", "they", "them", "him", "her", "you", "we", "it", "someone",
])
def test_pronouns_are_not_people(name):
    assert is_referential_name(name) is False


@pytest.mark.parametrize("name", ["", "   ", None, "A", "1", ")", "((", "—"])
def test_fragments_are_not_people(name):
    assert is_referential_name(name) is False


@pytest.mark.parametrize("name", [
    "Ли",
    "Ли Визардиум", "Дима", "Igor", "Валерий Железкин",
    "Maria Ivanova", "AB", "Оля",
])
def test_real_names_pass(name):
    assert is_referential_name(name) is True


def test_self_token_never_reaches_this_guard():
    assert "я" in SELF_TOKENS
    assert is_referential_name("я") is False
