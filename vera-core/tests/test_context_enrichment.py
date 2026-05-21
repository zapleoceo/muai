"""Tests for the retrieval relevance gate (veranda-leak fix) and new
filter predicates (folder, mutual_chat_contains)."""
from vera_shared.sources import evaluate
from app.triage.engine import _is_relevant, _tokenize


def test_tokenize_basic():
    tokens = _tokenize("Привет Марина, как дела с проектом")
    assert "марина" in tokens
    assert "проектом" in tokens
    # short stopwords filtered
    assert "и" not in tokens
    assert "с" not in tokens


def test_relevance_drops_unrelated_when_no_entity_match():
    # Marina sending a message about coffee. Graph fact about veranda.my
    # domain has zero token overlap, zero entity overlap → must drop.
    fact = "domain veranda.my is registered through registrar namecheap"
    query_tokens = _tokenize("Марина пишет про кофейню и встречу")
    entity_terms = {"марина", "ollushka90"}
    assert not _is_relevant(fact, query_tokens, entity_terms)


def test_relevance_keeps_instruction_episodes_regardless():
    # Persona signals are kept even if no token overlap.
    fact = "Дима написал инструкцию боту: игнорируй verandamybot"
    query_tokens = _tokenize("Марина пишет про кофейню")
    entity_terms = {"марина"}
    assert _is_relevant(fact, query_tokens, entity_terms)


def test_relevance_keeps_entity_mention():
    fact = "Ольга Олеговая написала про планы на вечер"
    query_tokens = _tokenize("Ольга предлагает встретиться завтра")
    entity_terms = {"ollushka90", "ольга"}
    assert _is_relevant(fact, query_tokens, entity_terms)


def test_folder_filter():
    rules = [{"match": {"folder": "Работа"}, "action": "include"}]
    assert evaluate(rules, {"folder": "Работа"}) == "include"
    assert evaluate(rules, {"folder": "Личное"}) == "exclude"


def test_folder_in_list():
    rules = [{"match": {"folder_in": ["Работа", "Команда"]}, "action": "include"}]
    assert evaluate(rules, {"folder": "Команда"}) == "include"
    assert evaluate(rules, {"folder": "Спам"}) == "exclude"


def test_folder_not_in():
    rules = [
        {"match": {"chat_type": "supergroup"}, "action": "include"},
        {"match": {"folder_not_in": ["Avoid"]}, "action": "include"},
        {"match": {"folder": "Avoid"}, "action": "exclude"},
    ]
    assert evaluate(rules,
                    {"chat_type": "supergroup", "folder": "Avoid"}) == "exclude"
    assert evaluate(rules,
                    {"chat_type": "supergroup", "folder": "Work"}) == "include"


def test_mutual_chat_contains():
    rules = [{"match": {"mutual_chat_contains": "веранда"}, "action": "priority"}]
    assert evaluate(rules,
                    {"mutual_chats": ["Веранда сотрудники", "Друзья"]}) == "priority"
    assert evaluate(rules,
                    {"mutual_chats": ["Друзья"]}) == "exclude"
