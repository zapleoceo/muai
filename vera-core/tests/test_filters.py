from vera_shared.sources import evaluate


def test_no_rules_default_exclude():
    assert evaluate(None, {"chat_type": "private"}) == "exclude"
    assert evaluate([], {"chat_type": "private"}) == "exclude"


def test_simple_include():
    rules = [{"match": {"chat_type": "private"}, "action": "include"}]
    assert evaluate(rules, {"chat_type": "private"}) == "include"
    assert evaluate(rules, {"chat_type": "channel"}) == "exclude"


def test_last_match_wins():
    rules = [
        {"match": {"chat_type": "private"}, "action": "include"},
        {"match": {"from_username": "spam"}, "action": "exclude"},
    ]
    assert evaluate(rules, {"chat_type": "private", "from_username": "alice"}) == "include"
    assert evaluate(rules, {"chat_type": "private", "from_username": "SPAM"}) == "exclude"


def test_chat_id_list_and_negation():
    rules = [
        {"match": {"chat_type": "group"}, "action": "include"},
        {"match": {"chat_id_not_in": [-100, -200]}, "action": "exclude"},
    ]
    assert evaluate(rules, {"chat_type": "group", "chat_id": -100}) == "include"
    assert evaluate(rules, {"chat_type": "group", "chat_id": -999}) == "exclude"


def test_priority_action():
    rules = [
        {"match": {"chat_type": "private"}, "action": "include"},
        {"match": {"text_contains": "urgent"}, "action": "priority"},
    ]
    assert evaluate(rules, {"chat_type": "private", "text": "this is URGENT"}) == "priority"


def test_text_regex_and_mention():
    rules = [
        {"match": {"mention_me": True}, "action": "include"},
        {"match": {"text_regex": r"^\s*ping"}, "action": "include"},
    ]
    assert evaluate(rules, {"mention_me": True, "text": "hi"}) == "include"
    assert evaluate(rules, {"mention_me": False, "text": "ping me"}) == "include"
    assert evaluate(rules, {"mention_me": False, "text": "say ping"}) == "exclude"


def test_unknown_predicate_fails_match():
    rules = [{"match": {"nonsense": "x", "chat_type": "private"}, "action": "include"}]
    assert evaluate(rules, {"chat_type": "private"}) == "exclude"
