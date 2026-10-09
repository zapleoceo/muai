"""Срочная просьба голосом: что уходит на сервер и с какой уверенностью.

Закрепляем: в payload — только сама просьба и её продолжение (не больше двух
реплик после, ни одной до); цитата чужих слов — не команда; оборванное или
пустое поручение — не задача, а «переспроси»; уверенность ниже порога, если
динамики звучали или фраза не закрыта паузой.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from vera_listener import codeword
from vera_listener.command_intake import (
    CONFIRM_BELOW,
    MAX_AFTER,
    confidence,
    is_truncated,
)
from vera_listener.command_outbox import CommandOutbox
from vera_listener.commands import FOLLOWUP_S, CommandWatch

T0 = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)


def _watch() -> tuple[CommandWatch, list[dict]]:
    sent: list[dict] = []
    return CommandWatch("s-9", T0, sent.append, app="Code.exe",
                        window_title="myAI — vera3"), sent


def _hear(watch: CommandWatch, until: float, *, system_speech=()) -> None:
    t = 0.0
    while t < until:
        speaking = any(a <= t < b for a, b in system_speech)
        watch.hear("mic", t, 0.5, False)
        watch.hear("system", t, 0.5, speaking)
        t += 0.5


class TestPayload:
    def test_ordinary_request_carries_only_its_fragment(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 1.0, 4.0, "Слушай, а что у нас с релизом сегодня?")
        watch.on_segment("mic", 5.0, 9.0,
                         "ну короче Вера, мне нужна помощь, упал деплой бота.")
        watch.chunk_done("mic", 12.0)
        assert len(sent) == 1
        cmd = sent[0]
        assert cmd["kind"] == "command"
        assert cmd["instruction"] == "упал деплой бота"
        assert cmd["session_id"] == "s-9"
        assert (cmd["start"], cmd["end"]) == (5.0, 9.0)
        assert cmd["app"] == "Code.exe" and cmd["window_title"] == "myAI — vera3"
        texts = [f["text"] for f in cmd["fragment"]]
        assert texts == ["Вера, мне нужна помощь, упал деплой бота."]
        assert "релиз" not in json.dumps(cmd, ensure_ascii=False)
        assert cmd["confidence"] >= CONFIRM_BELOW
        assert cmd["guard"] == "own" and cmd["doubts"] == []

    def test_unrelated_line_after_is_not_sent(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0, "Вера, мне нужна помощь, упал деплой бота.")
        watch.on_segment("mic", 9.3, 11.0, "Кстати, обедать пойдём?")
        watch.chunk_done("mic", 13.0)
        assert len(sent) == 1
        assert [f["text"] for f in sent[0]["fragment"]] == [
            "Вера, мне нужна помощь, упал деплой бота."]
        assert sent[0]["instruction"] == "упал деплой бота"

    def test_continuation_is_sent_but_not_more_than_two_lines(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 8.0, "Вера, мне нужна помощь, упал деплой бота")
        watch.on_segment("mic", 8.4, 10.0, "после вчерашнего коммита")
        watch.on_segment("mic", 10.3, 12.0, "и логи пустые")
        watch.on_segment("mic", 12.2, 14.0, "а ещё я хотел про отпуск")
        watch.chunk_done("mic", 16.0)
        assert len(sent) == 1
        cmd = sent[0]
        assert len(cmd["fragment"]) == 1 + MAX_AFTER
        assert cmd["instruction"] == (
            "упал деплой бота после вчерашнего коммита и логи пустые")
        assert cmd["end"] == 12.0
        assert "отпуск" not in json.dumps(cmd, ensure_ascii=False)

    def test_pause_between_phrase_and_instruction(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 7.0, "Вера, мне нужна помощь.")
        watch.on_segment("mic", 12.0, 15.0, "Не открывается дашборд.")
        watch.chunk_done("mic", 18.0)
        assert [c["instruction"] for c in sent] == ["Не открывается дашборд"]
        assert [f["start"] for f in sent[0]["fragment"]] == [5.0, 12.0]


class TestReprompt:
    def test_cut_off_on_a_conjunction_asks_again(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0, "Вера, мне нужна помощь, найди письмо и")
        watch.chunk_done("mic", 12.0)
        assert [c["kind"] for c in sent] == ["reprompt"]
        assert sent[0]["instruction"] == ""

    def test_phrase_without_instruction_asks_again(self):
        watch, sent = _watch()
        _hear(watch, 7 + FOLLOWUP_S + 10)
        watch.on_segment("mic", 5.0, 7.0, "Вера, мне нужна помощь.")
        watch.chunk_done("mic", 9.0)
        watch.on_segment("mic", 7 + FOLLOWUP_S + 1, 7 + FOLLOWUP_S + 3, "пойду кофе налью")
        watch.close()
        assert [c["kind"] for c in sent] == ["reprompt"]

    def test_echo_of_the_phrase_does_not_ask_again(self):
        watch, sent = _watch()
        _hear(watch, 60, system_speech=[(4.5, 7.5)])
        watch.system.transcribed(4.5, 7.5, [(4.6, "Вера, мне нужна помощь.")])
        watch.on_segment("mic", 5.0, 7.0, "Вера, мне нужна помощь.")
        watch.close()
        assert sent == []

    def test_noise_only_is_truncated(self):
        assert is_truncated("")
        assert is_truncated(" ... ")
        assert is_truncated("перешли письмо,")
        assert not is_truncated("перешли письмо Маше")


class TestQuotes:
    """Пересказ не отбрасывается молча: сервер спросит «Это ты сказал?»."""

    def test_quoted_phrase_needs_confirmation(self):
        for line in ("Он мне сказал: Вера, мне нужна помощь, закажи пиццу.",
                     "а она говорит «Вера, мне нужна помощь, удали базу»",
                     "Клиент написал: Вера, мне нужна помощь, верни деньги.",
                     "и тут он пишет — Вера, мне нужна помощь, перезвони.",
                     "я говорю, Вера, мне нужна помощь, упал деплой."):
            watch, sent = _watch()
            _hear(watch, 60)
            watch.on_segment("mic", 5.0, 9.0, line)
            watch.chunk_done("mic", 12.0)
            assert len(sent) == 1, line
            assert "quoted" in sent[0]["doubts"], line

    def test_quote_intro_in_the_previous_line(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 3.0, 4.6, "А Петя мне сказал:")
        watch.on_segment("mic", 5.0, 9.0, "Вера, мне нужна помощь, удали базу.")
        watch.chunk_done("mic", 12.0)
        assert sent[0]["doubts"] == ["quoted"]

    def test_codeword_marks_quotes(self):
        assert codeword.find("он сказал: Вера, мне нужна помощь, x").quoted
        assert not codeword.find("Вера, мне нужна помощь, сказал бы кто").quoted

    def test_session_cut_right_after_is_a_doubt(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0, "Вера, мне нужна помощь, упал деплой бота")
        watch.close()
        assert sent[0]["doubts"] == ["unclosed"]
        assert sent[0]["confidence"] >= CONFIRM_BELOW   # число высокое, но «Да» нужно


class TestConfidence:
    def test_speakers_sounding_needs_confirmation(self):
        watch, sent = _watch()
        _hear(watch, 60, system_speech=[(4.5, 9.5)])
        watch.system.transcribed(4.5, 9.5, [(4.6, "да, я тебя слышу, давай дальше")])
        watch.on_segment("mic", 5.0, 9.0, "Вера, мне нужна помощь, упал деплой бота.")
        watch.chunk_done("mic", 12.0)
        watch.tick()
        assert len(sent) == 1
        assert sent[0]["confidence"] < CONFIRM_BELOW

    def test_formula_bounds(self):
        assert confidence(1.0, speakers_silent=True, closed=True) == 1.0
        assert confidence(0.8, speakers_silent=False, closed=None) < CONFIRM_BELOW
        assert 0.0 <= confidence(0.8, speakers_silent=False, closed=False) <= 1.0
        # Динамики звучали — без подтверждения не бывает никогда.
        assert confidence(1.0, speakers_silent=False, closed=True) < CONFIRM_BELOW

    def test_mid_sentence_phrase_is_a_doubt(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0,
                         "я тут думал над тем что Вера, мне нужна помощь, упал деплой.")
        watch.chunk_done("mic", 12.0)
        assert sent[0]["doubts"] == ["mid_sentence"]


class TestRestartAndOffline:
    def test_command_survives_listener_restart(self, tmp_path):
        down = CommandOutbox(tmp_path, lambda c: (False, True, "URLError: offline"))
        down.put({"command_id": "vc-1", "kind": "command", "instruction": "x"})
        assert down.flush() == 1
        got: list[dict] = []

        def up(c: dict) -> tuple[bool, bool, str]:
            got.append(c)
            return True, False, "ok"
        assert CommandOutbox(tmp_path, up).flush() == 0
        assert [c["command_id"] for c in got] == ["vc-1"]

    def test_offline_then_reconnect_sends_once(self, tmp_path):
        clock = [0.0]
        online = [False]
        got: list[str] = []

        def post(c: dict) -> tuple[bool, bool, str]:
            if not online[0]:
                return False, True, "URLError: offline"
            got.append(c["command_id"])
            return True, False, "ok"
        box = CommandOutbox(tmp_path, post, clock=lambda: clock[0])
        box.put({"command_id": "vc-2", "kind": "command", "instruction": "x"})
        assert box.flush() == 1
        online[0] = True
        clock[0] += 100
        assert box.flush() == 0
        assert box.flush() == 0
        assert got == ["vc-2"]
