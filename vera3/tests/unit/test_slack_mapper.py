"""Slack: сообщение → событие. Разметка, авторство, отсев шума."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "ingestor-slack", "src"))

from ingestor_slack.mapper import (  # noqa: E402
    is_noise,
    message_to_event,
    parse_ts,
    unwrap,
)

ME = "U0ME"
NAMES = {"U0ME": "Дима", "UKOL": "Коля Петров", "UANN": "Аня"}


def _event(**over):
    message = {"ts": "1756200000.000100", "user": "UKOL", "text": "привет", **over}
    return message_to_event(
        message, channel_id="C1", channel_name="general", channel_kind="channel",
        is_private=False, me_id=ME, account="acme/dima", names=NAMES,
    )


class TestUnwrap:
    def test_user_reference_becomes_name(self):
        assert unwrap("<@UKOL> глянь", NAMES) == "@Коля Петров глянь"

    def test_unknown_user_falls_back_to_id(self):
        assert unwrap("<@UZZZ>", NAMES) == "@UZZZ"

    def test_inline_label_wins_over_cache(self):
        assert unwrap("<@UKOL|kolya>", NAMES) == "@kolya"

    def test_channel_reference(self):
        assert unwrap("см. <#C99|deploys>", NAMES) == "см. #deploys"

    def test_link_keeps_both_label_and_url(self):
        assert unwrap("<https://a.b/c|тут>", NAMES) == "тут (https://a.b/c)"

    def test_bare_link(self):
        assert unwrap("<https://a.b/c>", NAMES) == "https://a.b/c"

    def test_broadcast_reference(self):
        assert unwrap("<!here> внимание", NAMES) == "@here внимание"

    def test_html_entities_are_decoded(self):
        assert unwrap("a &lt;b&gt; &amp; c", NAMES) == "a <b> & c"


class TestNoise:
    def test_join_leave_is_noise(self):
        assert is_noise({"subtype": "channel_join"}) is True

    def test_bot_id_is_noise(self):
        """Slack — самая ботовая среда из подключённых: CI, алерты, Zapier.
        Без этого фильтра повторилась бы история с @leomatchbot."""
        assert is_noise({"bot_id": "B123", "text": "deploy ok"}) is True

    def test_bot_message_subtype_is_noise(self):
        assert is_noise({"subtype": "bot_message"}) is True

    def test_plain_human_message_is_not_noise(self):
        assert is_noise({"user": "UKOL", "text": "привет"}) is False


class TestMapping:
    def test_source_event_id_is_channel_plus_ts(self):
        assert _event()["source_event_id"] == "C1:1756200000.000100"

    def test_ts_becomes_naive_utc(self):
        expected = datetime.fromtimestamp(1756200000, tz=timezone.utc).replace(tzinfo=None)
        assert parse_ts("1756200000.000100") == expected

    def test_authorship_contract_first_line(self):
        spec = _event()
        assert spec["content_text"].startswith("Author: Коля Петров [counterparty]")
        assert spec["metadata_"]["author_role"] == "counterparty"
        assert spec["metadata_"]["author_label"] == "Коля Петров"

    def test_own_message_is_self(self):
        spec = _event(user=ME)
        assert spec["content_text"].startswith("Author: Я [self]")
        assert spec["metadata_"]["author_role"] == "self"
        assert spec["entity_hints"] == []

    def test_sender_id_is_written_for_the_graph(self):
        """ingest.authorship ищет алиас автора по sender_id — общая форма
        с telegram/instagram. Без него весь входящий Slack повис бы на Диме."""
        assert _event()["metadata_"]["sender_id"] == "UKOL"

    def test_counterparty_gets_entity_hint(self):
        assert _event()["entity_hints"] == [
            {"type": "person", "identifier": "user:UKOL", "name": "Коля Петров"}]

    def test_thread_reply_is_categorised_as_thread(self):
        spec = _event(thread_ts="1756100000.000001")
        assert spec["category"] == "thread"
        assert spec["metadata_"]["in_thread"] is True
        assert "(тред)" in spec["content_text"]

    def test_thread_parent_is_not_a_reply(self):
        spec = _event(thread_ts="1756200000.000100")
        assert spec["category"] == "channel"
        assert spec["metadata_"]["in_thread"] is False

    def test_dm_is_labelled_as_dm_not_channel_name(self):
        spec = message_to_event(
            {"ts": "1756200000.000100", "user": "UKOL", "text": "приветик"},
            channel_id="D1", channel_name="Коля Петров", channel_kind="im",
            is_private=True, me_id=ME, account="acme/dima", names=NAMES)
        assert spec["category"] == "im"
        assert "Where: ЛС" in spec["content_text"]

    def test_files_are_listed_in_body(self):
        spec = _event(text="", files=[{"name": "смета.xlsx"}, {"title": "фото"}])
        assert "[файлы] смета.xlsx, фото" in spec["content_text"]

    def test_reaction_only_message_still_saved(self):
        spec = _event(text="", reactions=[{"name": "+1"}])
        assert spec is not None
        assert spec["metadata_"]["reactions"] == ["+1"]

    def test_empty_message_is_dropped(self):
        assert _event(text="") is None

    def test_message_without_ts_is_dropped(self):
        assert _event(ts="") is None

    def test_bot_message_is_dropped(self):
        assert _event(bot_id="B1") is None

    def test_body_is_capped(self):
        spec = _event(text="я" * 20000)
        assert len(spec["content_text"]) == 8000
