"""vera_shared.graph.rel_policy — звать ли rel-extract, решается без LLM.

Замер 2026-09-13: ~855 вызовов в неделю, ноль связей с 2026-09-04; на
реальных событиях модели честно отвечали []. Гейт обязан резать новости,
уведомления систем и транскрипты ассистентов, но пропускать переписку, где
об отношениях действительно говорят.
"""
from __future__ import annotations

import pytest
from vera_shared.graph.rel_policy import (
    SKIP_CHANNEL,
    SKIP_MACHINE_SENDER,
    SKIP_NO_MARKER,
    SKIP_NO_PARTICIPATION,
    SKIP_SHORT,
    SKIP_SOURCE,
    has_relation_marker,
    is_machine_sender,
    rel_extract_skip_reason,
    should_extract_relations,
)
from vera_shared.ingest.envelope import message_body

REL_TEXT = "Моя жена Оля с весны работает в IT STEP менеджером"


def _wrap(chat: str, text: str) -> str:
    return f"Author: @x [counterparty]\nFrom: X\nChat: {chat} (user)\n---\n{text}"


class TestChats:
    def test_channel_posts_never_feed_the_graph(self):
        """Инцидент 2026-08-06: из рекламы SUP-тура в канале родилось
        `Дима -[client_of]-> T2T`, хотя имени в тексте не было вовсе."""
        assert rel_extract_skip_reason(
            "telegram", {"chat_kind": "channel"}, REL_TEXT) == SKIP_CHANNEL

    def test_group_without_participation(self):
        assert rel_extract_skip_reason(
            "telegram", {"chat_kind": "group", "owner_participates": False},
            REL_TEXT) == SKIP_NO_PARTICIPATION

    @pytest.mark.parametrize("meta", [
        {"chat_kind": "group", "owner_participates": True},
        {"chat_kind": "private"},
        {"chat_kind": "group"},      # легаси-событие без поля участия
        {},
        None,
    ])
    def test_real_correspondence_passes(self, meta):
        assert should_extract_relations("telegram", meta, REL_TEXT) is True


class TestSources:
    @pytest.mark.parametrize("source", ["claude_chat", "vera_chat"])
    def test_assistant_transcripts_are_skipped(self, source):
        assert rel_extract_skip_reason(source, {}, REL_TEXT) == SKIP_SOURCE

    @pytest.mark.parametrize("source", ["voice", "slack", "vera_memory", "manual"])
    def test_personal_sources_pass(self, source):
        assert rel_extract_skip_reason(source, None, REL_TEXT) is None


class TestMachineSender:
    @pytest.mark.parametrize("meta", [
        {"from": '"Olga Kryachko (JIRA)" <jira@itstep.atlassian.net>'},
        {"from": "Viktor <jira@itstep.atlassian.net>"},
        {"from": "Anthropic <no-reply-x@mail.anthropic.com>"},
        {"from": "Google Calendar <calendar-notification@google.com>"},
        {"sender_username": "autopay_telebot"},
        {"is_bot": True},
    ])
    def test_systems_are_machine_senders(self, meta):
        assert is_machine_sender(meta) is True
        assert rel_extract_skip_reason("gmail", meta, REL_TEXT) == SKIP_MACHINE_SENDER

    @pytest.mark.parametrize("meta", [
        {"from": "Ruslan Kovtiukh <kovtyukh_r@itstep.org>"},
        {"sender_username": "zapleosoft"},
        {"from": ""},
        {},
        None,
    ])
    def test_people_are_not(self, meta):
        assert is_machine_sender(meta) is False


class TestText:
    def test_short_message_after_header_is_skipped(self):
        """Длина считается по телу: шапка ингестора сама по себе длиннее 30."""
        assert rel_extract_skip_reason(
            "telegram", {}, _wrap("Оля", "ок, жена")) == SKIP_SHORT

    def test_marker_in_header_does_not_count(self):
        """Название чата «Веранда сотрудники» делало «сотрудником» каждого."""
        body = _wrap("Веранда сотрудники", "Киев под атакой БПЛА, все в укрытия")
        assert rel_extract_skip_reason("telegram", {}, body) == SKIP_NO_MARKER

    def test_marker_in_body_passes(self):
        assert rel_extract_skip_reason("telegram", {}, _wrap("Оля", REL_TEXT)) is None

    @pytest.mark.parametrize("text", [
        "Игорь работает в Sintegrum", "мой начальник уехал", "з дружиною",
        "вона працює в IT STEP", "she works at Google", "my wife said",
        "suami saya bekerja di Jakarta", "ЗП Оля 500 000", "мой друг Паша",
    ])
    def test_markers_found(self, text):
        assert has_relation_marker(text) is True

    @pytest.mark.parametrize("text", [
        "С завтрашнего дня по трекеру", "другой вариант", "обработка заказа",
        "Киев под атакой БПЛА", "", None,
    ])
    def test_non_markers(self, text):
        assert has_relation_marker(text) is False


def test_message_body_strips_header():
    assert message_body("Author: x\n---\nтело") == "тело"
    assert message_body("без шапки") == "без шапки"
    assert message_body(None) == ""
