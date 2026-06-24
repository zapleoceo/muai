"""Тесты temporal-парсера и весов источников (фикс «саммари за вчера»)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "brain-search", "src",
))

from brain_search.query_parse import (  # noqa: E402
    SOURCE_PROMPT_NOTE,
    TZ_OFFSET_H,
    extract_account_terms,
    is_summary_query,
    parse_time_range,
    resolve_project,
    source_weight,
)

# Фиксированное «сейчас»: 2026-06-10 03:42 UTC = 10:42 Jakarta
NOW = datetime(2026, 6, 10, 3, 42, 0)


def _local_date(dt_utc: datetime) -> str:
    return (dt_utc + timedelta(hours=TZ_OFFSET_H)).strftime("%Y-%m-%d")


class TestRelativeDays:
    def test_yesterday(self):
        rng = parse_time_range("дай саммари что было сделано вчера по Itstep", now_utc=NOW)
        assert rng is not None
        start, end = rng
        assert _local_date(start) == "2026-06-09"
        assert (end - start) == timedelta(days=1)

    def test_today(self):
        rng = parse_time_range("что пришло сегодня?", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-10"

    def test_day_before_yesterday(self):
        rng = parse_time_range("что было позавчера", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-08"

    def test_no_temporal_returns_none(self):
        assert parse_time_range("кто такой Дмитрий Егоров?", now_utc=NOW) is None

    def test_jakarta_midnight_boundary(self):
        """01:00 UTC = 08:00 Jakarta — «вчера» должно быть 9-е, не 8-е."""
        now = datetime(2026, 6, 10, 1, 0, 0)
        rng = parse_time_range("вчера", now_utc=now)
        assert _local_date(rng[0]) == "2026-06-09"

    def test_late_utc_evening_is_next_jakarta_day(self):
        """20:00 UTC 9-го = 03:00 Jakarta 10-го — «сегодня» = 10-е."""
        now = datetime(2026, 6, 9, 20, 0, 0)
        rng = parse_time_range("сегодня", now_utc=now)
        assert _local_date(rng[0]) == "2026-06-10"


class TestExplicitDates:
    def test_word_date(self):
        rng = parse_time_range("что было 9 июня?", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-09"

    def test_future_word_date_rolls_to_last_year(self):
        rng = parse_time_range("что было 25 декабря?", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2025-12-25"

    def test_iso_date(self):
        rng = parse_time_range("выгрузи 2026-06-09", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-09"

    def test_dotted_date(self):
        rng = parse_time_range("отчёт за 09.06.2026", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-09"

    def test_dotted_date_short_year(self):
        rng = parse_time_range("отчёт за 09.06.26", now_utc=NOW)
        assert rng is not None
        assert _local_date(rng[0]) == "2026-06-09"

    def test_invalid_date_returns_none(self):
        assert parse_time_range("что было 32.13.2026", now_utc=NOW) is None


class TestPeriods:
    def test_week(self):
        rng = parse_time_range("саммари за неделю", now_utc=NOW)
        assert rng is not None
        start, end = rng
        assert (end - start) == timedelta(days=8)  # 7 дней назад + сегодня

    def test_n_days(self):
        rng = parse_time_range("что было за 3 дня", now_utc=NOW)
        assert rng is not None
        start, end = rng
        assert (end - start) == timedelta(days=4)

    def test_month(self):
        rng = parse_time_range("сводка за месяц", now_utc=NOW)
        assert rng is not None
        start, end = rng
        assert (end - start) == timedelta(days=31)


class TestSourceWeights:
    def test_perplexity_downweighted(self):
        assert source_weight("perplexity") < 0.5

    def test_vera_chat_downweighted(self):
        assert source_weight("vera_chat") < 1.0

    def test_real_sources_full_weight(self):
        assert source_weight("gmail") == 1.0
        assert source_weight("telegram") == 1.0
        assert source_weight("instagram") == 1.0

    def test_vera_memory_boosted(self):
        assert source_weight("vera_memory") > 1.0

    def test_scoring_order_flips(self):
        """Главный сценарий бага: perplexity-событие с высоким FTS rank
        должно проиграть gmail-событию с сопоставимым rank'ом."""
        perplexity_score = 0.8 * source_weight("perplexity")
        gmail_score = 0.5 * source_weight("gmail")
        assert gmail_score > perplexity_score


class TestPromptNote:
    def test_note_mentions_perplexity_not_done(self):
        assert "perplexity" in SOURCE_PROMPT_NOTE
        assert "НЕ выполненная работа" in SOURCE_PROMPT_NOTE


class TestAccountTerms:
    """Извлечение имён собственных для match по account (фикс Itstep)."""

    def test_itstep_extracted_even_when_late_in_query(self):
        # Главный баг: «Itstep» 6-е значимое слово, generic забили слоты.
        words = ["саммари", "было", "сделано", "вчера", "проекту", "Itstep"]
        assert "itstep" in extract_account_terms(words)

    def test_generic_russian_words_excluded(self):
        words = ["саммари", "вчера", "проекту", "сделано"]
        assert extract_account_terms(words) == []

    def test_latin_lowercase_matched(self):
        assert "veranda" in extract_account_terms(["отчёт", "veranda"])

    def test_capitalized_russian_proper_noun_matched(self):
        assert "марина" in extract_account_terms(["Марина", "писала"])

    def test_short_words_skipped(self):
        # «Лиз» (3 символа) — слишком коротко, мусорные account-match
        assert extract_account_terms(["Лиз", "abc"]) == []

    def test_limit_respected(self):
        words = [f"Word{i}" for i in range(10)]
        assert len(extract_account_terms(words, limit=3)) == 3


class TestProjectResolution:
    def test_itstep_resolved(self):
        p = resolve_project("саммари по проекту Itstep за сегодня")
        assert p is not None and p.name == "itstep"
        assert any("itstep.org" in a for a in p.account_like)
        assert "J Branch Internal" in p.chats

    def test_itstep_via_jakarta(self):
        p = resolve_project("что нового по Джакарте?")
        assert p is not None and p.name == "itstep"

    def test_veranda_resolved(self):
        p = resolve_project("как дела в Веранде?")
        assert p is not None and p.name == "veranda"
        assert "Veranda менеджмент" in p.chats

    def test_no_project_returns_none(self):
        assert resolve_project("кто такой Дмитрий Егоров?") is None

    def test_chats_are_concrete_titles(self):
        p = resolve_project("itstep")
        # Чаты — точные title для exact-match по metadata->>'chat_title'
        assert all(isinstance(c, str) and c for c in p.chats)


class TestSummaryIntent:
    @pytest.mark.parametrize("q", [
        "дай саммари за сегодня",
        "что было сделано по проекту",
        "вытяни все переписки",
        "что полезного сегодня сделано",
        "итог дня",
        "сводка за неделю",
    ])
    def test_summary_detected(self, q):
        assert is_summary_query(q) is True

    @pytest.mark.parametrize("q", [
        "кто такой Егоров?",
        "какой телефон у Марины?",
        "когда встреча с Citra?",
    ])
    def test_non_summary(self, q):
        assert is_summary_query(q) is False
