"""gateway.voice — приём разговора с ноутбука.

Два свойства, которые тут закрепляются:

1. дословная расшифровка НЕ должна попадать в событие — в мозг идёт выжимка,
   сырой текст остаётся на ноутбуке;
2. длинный разговор СВОРАЧИВАЕТСЯ по окнам, а не обрезается. До 2026-08-26
   здесь был срез `[:60_000]` без лога — у двухчасовой встречи молча пропадал
   хвост, то есть ровно та часть, где договорённости.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from gateway.voice import Utterance, VoiceSession, body_text
from gateway.voice_distill import EMPTY as vd_EMPTY
from gateway.voice_distill import distill, render, windows
from vera_shared.llm import fold as fold_mod

_T0 = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)

_FULL = {"summary": "Обсудили цену выхода.", "counterparts": ["Ли"],
         "topics": ["Veranda"], "outline": ["сверили раздел 6", "сошлись на 50к"],
         "decisions": [], "commitments": [], "numbers": ["50 000"], "key_quotes": []}


def _session(**over):
    base = {
        "started_at": _T0, "ended_at": _T0 + timedelta(minutes=12),
        "app": "telegram.exe", "window_title": "Ли — Telegram",
        "utterances": [
            Utterance(at=0.0, stream="mic", text="Ли, по разделу шесть — сорок пять?"),
            Utterance(at=4.0, stream="system", text="Пусть будет пятьдесят, до пятницы."),
        ],
    }
    base.update(over)
    return VoiceSession(**base)


class _Sess:
    """Сессия-заглушка: запоминает параметры INSERT и отдаёт заданный id."""

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


class TestRender:
    def test_marks_who_is_speaking(self):
        lines = render(_session().utterances)
        assert lines[0].startswith("[я] Ли, по разделу шесть")
        assert lines[1].startswith("[собеседник] Пусть будет пятьдесят")

    def test_skips_empty_utterances(self):
        u = [Utterance(at=0, stream="mic", text="   "),
             Utterance(at=1, stream="system", text="есть")]
        assert render(u) == ["[собеседник] есть"]


class TestWindows:
    def test_short_transcript_is_one_window(self):
        chunks, truncated = windows(["раз", "два"], limit=100)
        assert (chunks, truncated) == (["раз\nдва"], False)

    def test_split_happens_on_utterance_boundaries(self):
        """Рвать реплику посередине нельзя — модель получит обрубок фразы."""
        chunks, _ = windows(["a" * 8, "b" * 8, "c" * 8], limit=20)
        assert chunks == ["a" * 8 + "\n" + "b" * 8, "c" * 8]

    def test_utterance_longer_than_window_is_kept_whole(self):
        chunks, _ = windows(["x" * 50], limit=10)
        assert chunks == ["x" * 50]

    def test_hitting_the_emergency_cap_is_reported(self):
        chunks, truncated = windows(["a" * 10] * 20, limit=10, max_windows=3)
        assert (len(chunks), truncated) == (3, True)

    def test_empty_input(self):
        assert windows([]) == ([], False)


class TestPools:
    def test_map_phase_runs_on_the_cheap_pool(self):
        """Обычный разговор — одно окно, то есть весь путь целиком.

        Замер 2026-08-27: `structured` 18с с той же точностью, `chat:smart`
        126с и не уложился даже в 120с ожидания брокера. Судить, что важно,
        всё равно оставляем умному — сборка частей идёт через него.
        """
        from gateway.voice_distill import SPEC
        assert SPEC.part_capability == "structured"
        assert SPEC.merge_capability == "chat:smart"


class TestBodyText:
    def test_keeps_structure_quotes_and_outline(self):
        d = {"summary": "Обсудили выход по разделу 6.",
             "counterparts": ["Ли"], "topics": ["Veranda", "выход"],
             "outline": ["сверили цифры", "договорились о сроке"],
             "decisions": ["цена 50к"], "commitments": ["Ли пришлёт бумаги до пятницы"],
             "numbers": ["50 000 USD", "пятница"],
             "key_quotes": ["Пусть будет пятьдесят, до пятницы."]}
        body = body_text(d, "telegram.exe", "Ли — Telegram")
        assert body.startswith("Обсудили выход по разделу 6.")
        for expected in ("Участники: Ли", "Решения: цена 50к",
                         "Договорённости: Ли пришлёт бумаги",
                         "Числа и сроки: 50 000 USD",
                         "Ход разговора:\n— сверили цифры",
                         "— Пусть будет пятьдесят",
                         "Где: telegram.exe / Ли — Telegram"):
            assert expected in body

    def test_omits_empty_sections(self):
        body = body_text({"summary": "Короткий звонок.", "counterparts": [],
                          "topics": [], "outline": [], "decisions": [],
                          "commitments": [], "numbers": [], "key_quotes": []},
                         None, None)
        assert body == "Короткий звонок."


class TestIngest:

    @pytest.mark.asyncio
    async def test_verbatim_never_reaches_the_event(self):

        secret_phrase = "Ли, по разделу шесть — сорок пять?"
        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            import gateway.voice as v
            res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

        assert res.ok and res.event_id == 777
        body = sess.params["content_text"]
        assert "Обсудили цену выхода." in body
        assert secret_phrase not in body          # дословное НЕ сохраняется
        assert sess.params["source"] == "voice"

    @pytest.mark.asyncio
    async def test_broker_failure_still_stores_the_fact(self):
        import gateway.voice as v
        from vera_shared.llm.client import LLMCallFailed

        sess = _Sess(event_id=5)
        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=LLMCallFailed("down"))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

        assert res.ok and res.event_id == 5
        assert "не удалось осмыслить" in sess.params["content_text"]
        assert sess.params["metadata"]["app"] == "telegram.exe"
        assert sess.params["metadata"]["distilled"] is False

    @pytest.mark.asyncio
    async def test_open_circuit_leaves_the_session_with_the_client(self):
        """Бюджет брокера кончился — не пишем пустышку: 500 вернёт сессию в
        офлайн-очередь слушателя, и звук не потеряется. LLMCallFailed — другое:
        там сохраняем хотя бы факт (см. тест выше)."""
        import gateway.voice as v
        from vera_shared.llm.client import LLMCoolingDown

        with patch.object(fold_mod, "chat_async",
                          AsyncMock(side_effect=LLMCoolingDown("chat:smart", remaining_s=60))),              patch("gateway.voice.get_session", lambda: _Sess()),              patch("gateway.voice.check_internal_secret", lambda s: None),              pytest.raises(LLMCoolingDown):
            await v.ingest_voice_session(_session(), x_internal_secret="ok")

    @pytest.mark.asyncio
    async def test_same_session_resent_is_deduped(self):
        import gateway.voice as v

        sess = _Sess(event_id=None)
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(vd_EMPTY), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

        assert res.ok and res.deduped and res.event_id is None

    @pytest.mark.asyncio
    async def test_transcript_size_is_recorded_in_metadata(self):
        """Молчаливая потеря недопустима: сколько было символов и сколько окон —
        видно в событии, а не только в логе."""
        import gateway.voice as v

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            await v.ingest_voice_session(_session(), x_internal_secret="ok")

        meta = sess.params["metadata"]
        assert meta["windows"] == 1
        assert meta["truncated"] is False
        assert meta["transcript_chars"] > 0

    @pytest.mark.asyncio
    async def test_parts_of_one_meeting_share_a_meeting_id(self):
        """Предохранитель режет трёхчасовую встречу, но связь остаётся —
        иначе в мозге это два независимых события."""
        import gateway.voice as v

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.voice.get_session", lambda: sess), \
             patch("gateway.voice.check_internal_secret", lambda s: None):
            await v.ingest_voice_session(
                _session(meeting_id="s-20260821T090000", part=2),
                x_internal_secret="ok")

        assert sess.params["metadata"]["meeting_id"] == "s-20260821T090000"
        assert sess.params["metadata"]["part"] == 2


class TestLongMeeting:

    def _many(self, n: int) -> list[Utterance]:
        return [Utterance(at=float(i), stream="mic" if i % 2 else "system",
                          text="реплика " + "я" * 200) for i in range(n)]

    @pytest.mark.asyncio
    async def test_long_transcript_is_folded_not_truncated(self):
        """Ключевой регресс. Раньше всё сверх 60k символов молча выбрасывалось;
        теперь каждое окно осмысляется, и последнее слово доходит до выжимки."""

        utterances = self._many(600)          # ≈ 130 тыс. символов
        prompts: list[str] = []

        async def fake_chat(**kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            return json.dumps({**_FULL, "summary": f"часть {len(prompts)}"}), {}

        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=fake_chat)):
            result, report = await distill(utterances, app="zoom.exe", title="Созвон")

        assert report["windows"] > 1
        assert report["truncated"] is False
        # Последний вызов — слияние; до него по одному вызову на окно.
        assert len(prompts) == report["windows"] + 1
        assert report["merged"] == "llm"
        # Ни одна реплика не потеряна: суммарный объём окон равен расшифровке.
        assert sum(len(p) for p in prompts[:-1]) > report["transcript_chars"]

    @pytest.mark.asyncio
    async def test_merge_failure_falls_back_to_mechanical_stitch(self):
        """Слить моделью не вышло — событие всё равно должно быть полным."""
        from vera_shared.llm.client import LLMCallFailed

        calls = {"n": 0}

        async def fake_chat(**kwargs):
            calls["n"] += 1
            if "--- части ---" in kwargs["messages"][0]["content"]:
                raise LLMCallFailed("merge down")
            return json.dumps({**_FULL, "topics": [f"тема{calls['n']}"]}), {}

        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=fake_chat)):
            result, report = await distill(self._many(600), app=None, title=None)

        assert report["merged"] == "mechanical"
        assert len(result["topics"]) == report["windows"]

    @pytest.mark.asyncio
    async def test_one_dead_window_does_not_lose_the_others(self):
        from vera_shared.llm.client import LLMCallFailed

        calls = {"n": 0}

        async def fake_chat(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise LLMCallFailed("window down")
            return json.dumps(_FULL), {}

        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=fake_chat)):
            result, report = await distill(self._many(600), app=None, title=None)

        assert report["parts"] == report["windows"] - 1
        assert result["summary"]
