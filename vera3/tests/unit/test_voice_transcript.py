"""Дословная стенограмма: хранится рядом с событием, но не в поиске.

Решение владельца 2026-08-27: звук не хранить, стенограмму хранить целиком,
вектор строить только по выжимке. Причина — необратимость: выжимка сжимает
разговор примерно в тридцать раз (замер: 66 445 символов расшифровки при
потолке content_text 8 000), а звук не сохраняется вообще, поэтому всё, что
модель сочла неважным, исчезало навсегда.

Тесты держат обе половины этого решения: дословное ЕСТЬ в content_extra и
дословного НЕТ в content_text.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from gateway.voice import Utterance, VoiceSession, transcript_record
from vera_shared.llm import fold as fold_mod

_T0 = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

_SUMMARY = {"summary": "Договорились о цене выхода.", "counterparts": ["Ли"],
            "topics": ["Veranda"], "outline": ["сошлись на 50к"],
            "decisions": [], "commitments": [], "numbers": ["50 000"],
            "key_quotes": []}

_SECRET = "Ли, по разделу шесть — сорок пять?"


def _session(**over):
    base = {
        "started_at": _T0, "ended_at": _T0 + timedelta(minutes=12),
        "app": "telegram.exe", "window_title": "Ли — Telegram",
        "utterances": [
            Utterance(at=0.0, stream="mic", text=_SECRET),
            Utterance(at=4.0, stream="system", text="Пусть будет пятьдесят."),
        ],
    }
    base.update(over)
    return VoiceSession(**base)


class _Sess:
    def __init__(self, event_id=777):
        self._event_id = event_id
        self.params: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self.params = stmt.compile().params
        outer = self

        class R:
            def scalar_one_or_none(self):
                return outer._event_id

        return R()


class TestRecord:
    def test_keeps_every_word_with_its_author(self):
        record = transcript_record(_session().utterances)
        assert record["kind"] == "voice_transcript"
        assert [u["stream"] for u in record["utterances"]] == ["mic", "system"]
        assert record["utterances"][0]["text"] == _SECRET

    def test_counts_characters_for_the_page_header(self):
        record = transcript_record(_session().utterances)
        assert record["chars"] == len(_SECRET) + len("Пусть будет пятьдесят.")

    def test_offsets_are_rounded_not_dropped(self):
        record = transcript_record([Utterance(at=12.3456, stream="mic", text="да")])
        assert record["utterances"][0]["at"] == 12.35

    def test_empty_session_gives_an_empty_transcript(self):
        record = transcript_record([])
        assert record["utterances"] == [] and record["chars"] == 0


class TestStored:
    @pytest.mark.asyncio
    async def test_verbatim_goes_to_content_extra(self):
        import gateway.voice as v

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_SUMMARY), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            await v.ingest_voice_session(_session(), x_internal_secret="ok")

        extra = sess.params["content_extra"]
        assert extra["kind"] == "voice_transcript"
        assert any(_SECRET in u["text"] for u in extra["utterances"])

    @pytest.mark.asyncio
    async def test_verbatim_still_never_reaches_content_text(self):
        """В вектор идёт только content_text — дословное там перебивало бы выжимку."""
        import gateway.voice as v

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_SUMMARY), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            await v.ingest_voice_session(_session(), x_internal_secret="ok")

        assert _SECRET not in sess.params["content_text"]
        assert "Договорились о цене выхода." in sess.params["content_text"]

    @pytest.mark.asyncio
    async def test_transcript_survives_a_failed_distillation(self):
        """Модель не справилась — дословное тем ценнее, его нельзя терять."""
        import gateway.voice as v
        from vera_shared.llm.client import LLMCallFailed

        sess = _Sess(event_id=5)
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(side_effect=LLMCallFailed("down"))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            await v.ingest_voice_session(_session(), x_internal_secret="ok")

        assert "не удалось осмыслить" in sess.params["content_text"]
        assert any(_SECRET in u["text"]
                   for u in sess.params["content_extra"]["utterances"])


class TestEchoSplit:
    """Эхо помечено слушателем: стенограмма берёт всё, осмысление — чистое.

    Микрофон слышит динамики, поэтому часть реплик дорожки `mic` — голос
    собеседника. Слушатель их не выбрасывает: в один кусок попадает и эхо, и
    слова владельца, а звук не хранится, значит выброшенное не вернуть.
    Разделение проходит здесь.
    """

    @staticmethod
    def _utterances():
        return [
            Utterance(at=0.0, stream="system", text="давай сверим сроки по проекту"),
            Utterance(at=1.0, stream="mic",
                      text="давай сверим сроки по проекту ага записал", echo=True),
            Utterance(at=8.0, stream="mic", text="во вторник пришлю смету"),
        ]

    def test_transcript_keeps_every_utterance(self):
        record = transcript_record(self._utterances())
        assert len(record["utterances"]) == 3
        assert record["echoes"] == 1
        assert any("ага записал" in u["text"] for u in record["utterances"])

    def test_flag_written_only_where_true(self):
        """Ключ у каждой реплики раздувал бы jsonb: эха около десятой части."""
        record = transcript_record(self._utterances())
        flagged = [u for u in record["utterances"] if "echo" in u]
        assert len(flagged) == 1
        assert flagged[0]["echo"] is True

    def test_chars_count_everything_including_echo(self):
        record = transcript_record(self._utterances())
        assert record["chars"] == sum(len(u.text) for u in self._utterances())

    def test_old_listener_without_the_field_still_works(self):
        """Слушатель может быть старее сервера — деплой идёт в этом порядке."""
        record = transcript_record([Utterance(at=0.0, stream="mic", text="привет")])
        assert record["echoes"] == 0
        assert "echo" not in record["utterances"][0]


class TestSpeakerNames:
    """Кто именно сказал реплику: дорожка `system` смешивает всех удалённых
    участников, и без имени в созвоне на пятерых видно только «не владелец»."""

    @staticmethod
    def _utterances():
        return [
            Utterance(at=0.0, stream="mic", text="я начну"),
            Utterance(at=2.0, stream="system", text="давай", speaker="Вадим"),
            Utterance(at=5.0, stream="system", text="я против", speaker="Виктор"),
            Utterance(at=8.0, stream="system", text="без имени"),
        ]

    def test_speaker_is_stored_per_utterance(self):
        record = transcript_record(self._utterances())
        by_at = {u["at"]: u for u in record["utterances"]}
        assert by_at[2.0]["speaker"] == "Вадим"
        assert by_at[5.0]["speaker"] == "Виктор"

    def test_key_is_written_only_where_the_name_is_known(self):
        """Ключ у каждой реплики раздувал бы jsonb: имя есть не всегда."""
        record = transcript_record(self._utterances())
        by_at = {u["at"]: u for u in record["utterances"]}
        assert "speaker" not in by_at[0.0]
        assert "speaker" not in by_at[8.0]

    def test_distinct_voices_are_listed(self):
        record = transcript_record(self._utterances())
        assert record["speakers"] == ["Вадим", "Виктор"]

    def test_no_names_gives_empty_list_not_missing_key(self):
        record = transcript_record([Utterance(at=0.0, stream="mic", text="привет")])
        assert record["speakers"] == []

    def test_old_listener_without_the_field_still_works(self):
        """Слушатель может быть старее сервера — деплой идёт в этом порядке."""
        record = transcript_record([Utterance(at=0.0, stream="system", text="привет")])
        assert record["speakers"] == []
        assert "speaker" not in record["utterances"][0]
