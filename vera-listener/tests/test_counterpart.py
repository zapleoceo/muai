"""Имя собеседника из заголовка окна.

Ошибка здесь дороже пустого результата: имя уходит в постоянное хранилище
отпечатков, и один раз ошибившись, слушатель будет годами звать чужим
именем. Поэтому тестов на «не угадал» больше, чем на «угадал».

Заголовки — настоящие, из лога 2026-09-02.
"""
from __future__ import annotations

from vera_listener.counterpart import (
    counterpart,
    counterpart_name,
    looks_like_person,
)


class TestSlack:
    def test_real_title_from_the_log(self):
        assert counterpart_name(
            "slack.exe", "Viktor Gavrylenko - Sintegrum Team - Slack"
        ) == "Viktor Gavrylenko"

    def test_dm_marker_is_stripped(self):
        assert counterpart_name(
            "slack.exe", "Viktor Gavrylenko (DM) - Sintegrum Team - Slack"
        ) == "Viktor Gavrylenko"

    def test_unread_counter_is_stripped(self):
        assert counterpart_name(
            "slack.exe",
            "Viktor Gavrylenko (DM) - Sintegrum Team - 1 new item - Slack"
        ) == "Viktor Gavrylenko"

    def test_cyrillic_name(self):
        assert counterpart_name(
            "slack.exe", "Вадим Кудрявцев - Sintegrum Team - Slack"
        ) == "Вадим Кудрявцев"

    def test_channel_is_not_a_person(self):
        """Канал — не человек: записать его голосом одного из участников
        значило бы приписать всем в канале один голос."""
        assert counterpart_name("slack.exe", "general - Sintegrum Team - Slack") is None

    def test_hyphenated_channel_is_not_a_person(self):
        assert counterpart_name("slack.exe", "dev-team - Sintegrum Team - Slack") is None


class TestTelegram:
    def test_plain_chat_name(self):
        assert counterpart_name("telegram.exe", "Оля Кричко") == "Оля Кричко"

    def test_unread_counter_is_stripped(self):
        assert counterpart_name("telegram.exe", "Оля Кричко (3)") == "Оля Кричко"

    def test_group_name_is_indistinguishable_and_that_is_admitted(self):
        """Регулярка тут бессильна: «Кайфушники Нячанга» и «Оля Кричко»
        выглядят одинаково. Имя для разметки берём, но права запомнить голос
        не даём — вся защита в `is_direct`, а не в форме строки."""
        found = counterpart("telegram.exe", "Кайфушники Нячанга")
        assert found is not None and found.name == "Кайфушники Нячанга"
        assert found.is_direct is False


class TestMeet:
    def test_bare_meet_gives_nothing(self):
        assert counterpart_name("chrome.exe", "Meet - Google Chrome") is None

    def test_meeting_topic_is_never_taken_as_a_name(self):
        """Реальный заголовок из лога. Принять его за имя значило бы записать
        голос человека под названием совещания."""
        assert counterpart_name(
            "chrome.exe", "Meet – Оркестра - статус апдейт - Google Chrome") is None

    def test_unrelated_chrome_window(self):
        assert counterpart_name("chrome.exe", "Transparent Window") is None


class TestAppAndTitleMustAgree:
    def test_slack_title_while_telegram_plays_audio_is_refused(self):
        """Видели вживую 2026-09-02: звук шёл из Telegram, а впереди было окно
        Slack. Взять имя из чужого заголовка значило бы назвать собеседника
        человеком из соседнего окна."""
        assert counterpart_name(
            "telegram.exe", "Viktor Gavrylenko (DM) - Sintegrum Team - Slack") is None

    def test_slack_app_with_foreign_title_is_refused(self):
        assert counterpart_name("slack.exe", "Meet - Google Chrome") is None

    def test_unknown_app_gives_nothing(self):
        assert counterpart_name("zoom.exe", "Viktor Gavrylenko") is None


class TestMissingInput:
    def test_no_app(self):
        assert counterpart_name(None, "Viktor Gavrylenko - Sintegrum Team - Slack") is None

    def test_no_title(self):
        assert counterpart_name("slack.exe", None) is None

    def test_empty_title(self):
        assert counterpart_name("slack.exe", "") is None


class TestLooksLikePerson:
    def test_two_capitalised_words(self):
        assert looks_like_person("Viktor Gavrylenko")

    def test_three_words_allowed(self):
        assert looks_like_person("Анна Мария Петрова")

    def test_single_word_is_not_enough(self):
        """Одно слово — это скорее ник, канал или название приложения."""
        assert not looks_like_person("general")

    def test_lowercase_is_rejected(self):
        assert not looks_like_person("viktor gavrylenko")

    def test_channel_hash_is_rejected(self):
        assert not looks_like_person("#general")

    def test_digits_are_rejected(self):
        assert not looks_like_person("Sprint 42")


class TestDirectMessageConfidence:
    """`is_direct` решает, можно ли ЗАПОМНИТЬ голос под этим именем."""

    def test_slack_person_is_a_confirmed_direct_message(self):
        """У Slack канал отличим по форме, значит имя человека = личка."""
        found = counterpart("slack.exe", "Viktor Gavrylenko - Sintegrum Team - Slack")
        assert found is not None and found.is_direct is True

    def test_telegram_is_never_confirmed(self):
        found = counterpart("telegram.exe", "Оля Кричко")
        assert found is not None and found.is_direct is False

    def test_meet_gives_nothing_at_all(self):
        assert counterpart("chrome.exe", "Meet - Google Chrome") is None
