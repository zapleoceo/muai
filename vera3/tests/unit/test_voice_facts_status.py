from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from bot_telegram.brain import ask_brain
from vera_shared.voice_help.fact_route import (
    PREFIX,
    external_facts,
    fresh_facts_action,
    fresh_result,
)

NOW = datetime(2026, 11, 6, 7, tzinfo=UTC)


@pytest.mark.parametrize("query,external", [
    ("Какой сейчас курс доллара к гривне?", True),
    ("Помнишь, какой сейчас курс доллара?", True),
    ("Сколько сейчас стоит доллар?", True),
    ("По какому курсу я менял доллары сегодня?", False),
    ("USD UAH exchange rate today", True),
    ("Какая погода сегодня?", True),
    ("Вспомни, по какому курсу я менял доллары вчера", False),
    ("Что я сегодня обсуждал с коллегами?", False),
    ("Посмотри логи бота", False),
])
def test_facts_are_separate_from_personal_recall(query, external):
    assert external_facts(query) is external


def report(**extra):
    data = {"answer": "Test observation, not a real quote", "available": True,
            "checked_at": NOW.isoformat(), "as_of": NOW.isoformat(),
            "source_url": "https://bank.gov.ua/", "rate_type": "official NBU"}
    return PREFIX + json.dumps({**data, **extra})


@pytest.mark.parametrize("extra", [
    {"checked_at": (NOW - timedelta(hours=1)).isoformat()},
    {"checked_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"as_of": (NOW - timedelta(days=2)).isoformat()},
    {"as_of": NOW.replace(tzinfo=None).isoformat()},
    {"source_url": "http://bank.gov.ua/"},
    {"source_url": "https://user:pass@bank.gov.ua/"},
    {"rate_type": ""}, {"available": "true"},
])
def test_unverified_or_old_observation_is_not_current(extra):
    assert fresh_result(report(**extra), NOW) is None


def test_freshness_is_measured_at_delivery_not_claimed_check_time():
    assert fresh_result(report(checked_at=(NOW - timedelta(minutes=29)).isoformat(),
                               as_of=(NOW - timedelta(hours=24, minutes=20)).isoformat()), NOW) is None


@pytest.mark.parametrize("kind,url,accepted", [
    ("cash buy", "https://bank.gov.ua/", False),
    ("cash sell", "https://example.com/", False),
    ("official NBU", "https://example.com/", False),
    ("official NBU", "https://bank.gov.ua.example.com/", False),
    ("official NBU", "https://bank.gov.ua/", True),
])
def test_only_explicit_official_nbu_has_daily_age_exception(kind, url, accepted):
    text = report(rate_type=kind, source_url=url, as_of=(NOW - timedelta(hours=2)).isoformat())
    assert (fresh_result(text, NOW) is not None) is accepted


def test_valid_and_unavailable_reports_are_explicit():
    assert "official NBU" in fresh_result(report(), NOW)
    missing = PREFIX + json.dumps({"answer": "Source refused access; no current value verified.",
                                    "available": False, "checked_at": NOW.isoformat()})
    assert "unavailable" in fresh_result(missing, NOW)
    assert fresh_result("Old memory says a value", NOW) is None


def test_handoff_carries_actual_reference_time_and_source_contract():
    action = fresh_facts_action("курс USD UAH", NOW)
    assert NOW.isoformat() in action and "FRESH_FACTS_V1" in action
    assert "no new access or paid calls" in action
    assert fresh_facts_action("Что я говорил вчера?", NOW) == ""


@pytest.mark.asyncio
async def test_current_fx_never_queries_personal_memory():
    with patch("bot_telegram.brain.httpx.AsyncClient") as client:
        answer = await ask_brain("Какой сейчас курс доллара к гривне?", 1, 1)
    client.assert_not_called()
    assert answer.provider == "external-source-required" and answer.n_results == 0
