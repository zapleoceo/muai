"""Поручения по ходу разговора: чей голос, когда решать, сколько раз отправлять.

Главное свойство — чужой голос командой не становится: ни с дорожки
`system`, ни эхом из динамиков в микрофоне. Остальное — чтобы поручение
владельца не терялось и не приходило дважды.
"""
from __future__ import annotations

from datetime import datetime, timezone

from vera_listener.commands import FOLLOWUP_S, CommandWatch

T0 = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
PHRASE = "Вера, мне нужна помощь, срочно напиши мне что-то в телеграм"


def _watch() -> tuple[CommandWatch, list[dict]]:
    sent: list[dict] = []
    return CommandWatch("s-1", T0, sent.append, app="zoom.exe",
                        window_title="Созвон"), sent


def _hear(watch: CommandWatch, until: float, *, system_speech=()) -> None:
    """Прогнать часы захвата до `until`; system_speech — отрезки речи в динамиках."""
    t = 0.0
    while t < until:
        speaking = any(a <= t < b for a, b in system_speech)
        watch.hear("mic", t, 0.5, False)
        watch.hear("system", t, 0.5, speaking)
        t += 0.5


class TestWhoseVoice:
    def test_owner_alone_is_accepted_right_away(self):
        watch, sent = _watch()
        _hear(watch, 20)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        assert len(sent) == 1
        assert sent[0]["instruction"] == "срочно напиши мне что-то в телеграм"
        assert sent[0]["spoken_at"] == "2026-09-30T10:00:05+00:00"
        assert sent[0]["command_id"].startswith("vc-")

    def test_system_track_never_gives_a_command(self):
        watch, sent = _watch()
        _hear(watch, 20)
        watch.on_segment("system", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []

    def test_echo_of_the_other_side_is_rejected(self):
        """Собеседник сказал фразу, микрофон поймал её из динамиков."""
        watch, sent = _watch()
        _hear(watch, 60, system_speech=[(4.5, 9.5)])
        watch.system.transcribed(4.5, 9.5, [(4.6, PHRASE)])
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []

    def test_waits_for_system_transcript_instead_of_guessing(self):
        watch, sent = _watch()
        _hear(watch, 60, system_speech=[(4.5, 9.5)])
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        assert sent == [] and watch.wants_system_text()
        watch.system.transcribed(4.5, 9.5, [(4.6, "да, я тебя слышу, давай дальше")])
        watch.tick()
        assert len(sent) == 1

    def test_unverifiable_on_close_is_not_executed(self):
        """Динамики звучали, а распознанного системного звука нет вовсе —
        доказать, что это владелец, нечем."""
        watch, sent = _watch()
        _hear(watch, 60, system_speech=[(4.5, 9.5)])
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []

    def test_decision_waits_until_the_window_is_heard(self):
        watch, sent = _watch()
        _hear(watch, 9.5)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        assert sent == []
        _hear(watch, 20)
        watch.tick()
        assert len(sent) == 1

    def test_ordinary_speech_sends_nothing(self):
        watch, sent = _watch()
        _hear(watch, 20)
        watch.on_segment("mic", 1.0, 3.0, "мне нужна помощь с отчётом")
        watch.on_segment("mic", 4.0, 6.0, "вера сказала, что придёт")
        watch.close()
        assert sent == []


class TestDeadLoopback:
    """Системная дорожка не даёт кадров (устройство отвалилось) — это не тишина."""

    def _mic_only(self, watch: CommandWatch, until: float) -> None:
        t = 0.0
        while t < until:
            watch.hear("mic", t, 0.5, True)
            t += 0.5

    def test_no_system_frames_is_not_owner(self):
        watch, sent = _watch()
        self._mic_only(watch, 30)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.tick()
        assert sent == []

    def test_no_system_frames_is_rejected_on_close_too(self):
        watch, sent = _watch()
        self._mic_only(watch, 30)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []

    def test_gap_in_system_frames_inside_window_is_not_owner(self):
        watch, sent = _watch()
        t = 0.0
        while t < 30:
            watch.hear("mic", t, 0.5, False)
            if not 6.0 <= t < 8.0:
                watch.hear("system", t, 0.5, False)
            t += 0.5
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []


class TestInstructionInNextLine:
    def test_instruction_comes_with_the_next_mic_line(self):
        watch, sent = _watch()
        _hear(watch, 40)
        watch.on_segment("mic", 5.0, 7.0, "Вера, мне нужна помощь.")
        assert sent == []
        watch.on_segment("system", 8.0, 9.0, "ага")
        watch.on_segment("mic", 9.0, 12.0, "Срочно напиши мне что-то в телеграм")
        assert [c["instruction"] for c in sent] == ["Срочно напиши мне что-то в телеграм"]

    def test_too_late_next_line_is_not_an_instruction(self):
        watch, sent = _watch()
        _hear(watch, 7 + FOLLOWUP_S + 5)
        watch.on_segment("mic", 5.0, 7.0, "Вера, мне нужна помощь.")
        watch.tick()
        watch.on_segment("mic", 7 + FOLLOWUP_S + 1, 7 + FOLLOWUP_S + 3,
                         "пойду налью кофе")
        watch.close()
        assert sent == []


class TestOneCommandOneMessage:
    def test_same_line_recognised_twice_is_sent_once(self):
        watch, sent = _watch()
        _hear(watch, 20)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert len(sent) == 1

    def test_repeated_request_shortly_after_is_a_duplicate(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.on_segment("mic", 20.0, 24.0, PHRASE.replace(",", ""))
        watch.close()
        assert len(sent) == 1

    def test_different_requests_are_both_sent(self):
        watch, sent = _watch()
        _hear(watch, 60)
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.on_segment("mic", 20.0, 24.0,
                         "Вера, мне нужна помощь, найди письмо от Ли про аренду")
        watch.close()
        assert len(sent) == 2
        assert sent[0]["command_id"] != sent[1]["command_id"]


def _frames(watch: CommandWatch, until: float, *, gaps=(), system_from: float = 0.0,
            ) -> None:
    """Кадры по 32 мс на обеих дорожках; `gaps` — (начало, длина) пропуска system."""
    step, t = 0.032, 0.0
    while t < until:
        watch.hear("mic", t, step, False)
        skipped = t < system_from or any(a <= t < a + d for a, d in gaps)
        if not skipped:
            watch.hear("system", t, step, False)
        t += step


class TestCaptureJitter:
    """Дрожание захвата — не слепота; мёртвое устройство — слепота."""

    def test_short_pauses_in_system_frames_are_still_owner(self):
        for pause in (0.1, 0.3, 0.8):
            watch, sent = _watch()
            _frames(watch, 30, gaps=[(4.0, pause), (7.0, pause), (11.0, pause)])
            watch.on_segment("mic", 5.0, 9.0, PHRASE)
            watch.close()
            assert len(sent) == 1, pause

    def test_long_gap_in_system_frames_is_not_owner(self):
        watch, sent = _watch()
        _frames(watch, 30, gaps=[(7.0, 1.5)])
        watch.on_segment("mic", 5.0, 9.0, PHRASE)
        watch.close()
        assert sent == []

    def test_command_in_first_seconds_of_session(self):
        """Loopback открылся на полсекунды позже микрофона — это не слепота."""
        watch, sent = _watch()
        _frames(watch, 30, system_from=0.5)
        watch.on_segment("mic", 1.0, 4.0, PHRASE)
        watch.tick()
        assert len(sent) == 1
