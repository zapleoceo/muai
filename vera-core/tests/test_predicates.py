"""Unit tests for trigger predicate evaluation."""
from app.triggers.predicates import matches, SUPPORTED_BY_SOURCE


def test_empty_predicate_matches_anything():
    assert matches(None, {"from": "x"}) is True
    assert matches({}, {"from": "x"}) is True


def test_from_contains_case_insensitive():
    ev = {"from": "Boss <boss@COMPANY.com>"}
    assert matches({"from_contains": "boss@company"}, ev) is True
    assert matches({"from_contains": "other"}, ev) is False


def test_multiple_keys_all_must_match():
    ev = {"from": "boss@x.com", "subject": "Urgent budget"}
    assert matches({"from_contains": "boss@", "subject_matches": "budget"}, ev) is True
    assert matches({"from_contains": "boss@", "subject_matches": "lunch"}, ev) is False


def test_amount_gt():
    assert matches({"amount_gt": 1000}, {"amount": 1500}) is True
    assert matches({"amount_gt": 1000}, {"amount": 500}) is False
    assert matches({"amount_gt": 1000}, {"amount": 0}) is False


def test_unknown_key_fails_closed():
    """Unknown predicate keys must NOT match by default — safer."""
    assert matches({"made_up_key": "anything"}, {"from": "x"}) is False


def test_each_source_advertises_at_least_one_predicate():
    for source, preds in SUPPORTED_BY_SOURCE.items():
        assert len(preds) > 0, f"{source} has no supported predicates"
        for p in preds:
            assert "key" in p and "label" in p and "input" in p
