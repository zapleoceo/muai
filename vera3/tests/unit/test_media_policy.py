"""vera_shared.media_policy — что идёт на распознавание и в граф связей.

Правило по участию владельца заменило денилист названий чатов (2026-08-27).
Главный регресс, который тесты обязаны держать: группа-обсуждение публичного
канала («Быть Или»: 1792 автора, ни одного своего сообщения) распознаваться
НЕ должна, а корпоративная группа со скромным участием («Jakarta sales»:
16 своих сообщений) — должна.
"""
from __future__ import annotations

import pytest
from vera_shared.media_policy import (
    SKIP_CHANNEL,
    SKIP_KIND,
    SKIP_NO_PARTICIPATION,
    classify_chat_kind,
    media_skip_reason,
    should_extract_relations,
    should_recognize_media,
)

MIN_OWN = 5


def recognize(media_kind, chat_kind, own=0):
    return should_recognize_media(media_kind, chat_kind, own_messages=own,
                                  min_own_messages=MIN_OWN)


def reason(media_kind, chat_kind, own=0):
    return media_skip_reason(media_kind, chat_kind, own_messages=own,
                             min_own_messages=MIN_OWN)


class TestKind:
    @pytest.mark.parametrize("media_kind,chat_kind", [
        # голос/аудио — всегда: дешёвый whisper-пул, самый ценный контент
        ("voice", "channel"), ("voice", "private"), ("audio", "group"),
    ])
    def test_voice_always_recognized(self, media_kind, chat_kind):
        assert recognize(media_kind, chat_kind, own=0) is True

    @pytest.mark.parametrize("media_kind", [
        "sticker", "video", "video_note", "document", None,
    ])
    def test_kinds_we_never_recognize(self, media_kind):
        assert reason(media_kind, "private", own=1000) == SKIP_KIND


class TestPhotos:
    def test_private_chat_always(self):
        assert reason("photo", "private", own=0) is None
        assert reason("image", "private", own=0) is None

    def test_broadcast_channel_never(self):
        """Новости и мемы: ноль ценности для личной памяти, жгут vision-бюджет."""
        assert reason("photo", "channel", own=0) == SKIP_CHANNEL
        assert reason("image", "channel", own=9999) == SKIP_CHANNEL

    def test_group_needs_participation(self):
        assert reason("photo", "group", own=MIN_OWN) is None
        assert reason("photo", "group", own=MIN_OWN - 1) == SKIP_NO_PARTICIPATION

    def test_public_discussion_group_of_a_channel(self):
        """«Быть Или»: megagroup, поэтому chat_kind=group и фильтр каналов её
        не ловил. 1792 автора, ни одного своего сообщения, 196 фото в очереди —
        четверть всей очереди."""
        assert reason("photo", "group", own=0) == SKIP_NO_PARTICIPATION

    def test_real_work_group_with_modest_participation(self):
        """«Jakarta sales» — 16 своих сообщений из 1063. Это работа."""
        assert reason("photo", "group", own=16) is None

    def test_unknown_chat_kind_needs_participation_too(self):
        """У старых событий chat_kind нет — решаем по участию, а не «пропустить»."""
        assert reason("photo", None, own=0) == SKIP_NO_PARTICIPATION
        assert reason("photo", None, own=50) is None

    def test_zero_threshold_lets_everything_through(self):
        """Настройка 0 — осознанный выбор «распознавать все группы»."""
        assert media_skip_reason("photo", "group", own_messages=0,
                                 min_own_messages=0) is None


class TestClassifyChatKind:
    def test_private(self):
        assert classify_chat_kind("user", False) == "private"

    def test_legacy_group(self):
        assert classify_chat_kind("chat", False) == "group"
        assert classify_chat_kind("chatfull", False) == "group"

    def test_supergroup_is_a_group_not_a_channel(self):
        """Telethon отдаёт супергруппу как Channel — без megagroup это был бы
        баг: 96% реальных групп сегодня супергруппы."""
        assert classify_chat_kind("channel", True) == "group"

    def test_broadcast_channel(self):
        assert classify_chat_kind("channel", False) == "channel"

    def test_unknown(self):
        assert classify_chat_kind("bot", False) == "other"
        assert classify_chat_kind("unknown", True) == "other"


class TestRelations:
    def test_channel_posts_never_feed_the_graph(self):
        """Инцидент 2026-08-06: из рекламы SUP-тура в канале родилось
        `Дима -[client_of]-> T2T`, хотя имени в тексте не было вовсе."""
        assert should_extract_relations({"chat_kind": "channel"}) is False
        assert should_extract_relations(
            {"chat_kind": "channel", "chat_title": "T2T | Афиша Нячанга"}) is False

    def test_group_without_participation_never_feeds_the_graph(self):
        """Чужая публичная болтовня личных фактов о владельце не несёт."""
        assert should_extract_relations(
            {"chat_kind": "group", "owner_participates": False}) is False

    @pytest.mark.parametrize("meta", [
        {"chat_kind": "group", "owner_participates": True},
        {"chat_kind": "private"},
        {"chat_kind": "supergroup", "owner_participates": True},
        {},      # метаданных нет — не наказываем, строим
        None,
    ])
    def test_real_correspondence_feeds_the_graph(self, meta):
        assert should_extract_relations(meta) is True

    def test_legacy_event_without_the_field(self):
        """У событий до 2026-08-27 поля нет — остаётся только проверка канала."""
        assert should_extract_relations({"chat_kind": "group"}) is True
